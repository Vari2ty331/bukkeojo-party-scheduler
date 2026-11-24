"""Offline tests for Discord-facing methods, using fake guilds and threads."""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from core.calendar import KST, title, legacy_thread_title
from core.storage import Storage

SOURCE = Path(__file__).resolve().parents[1] / 'bot.py'
START = date(2026, 11, 26)
NOW = datetime(2026, 11, 24, 12, tzinfo=KST)


def bot_method(name, **namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace[name]


class NicknameTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_names_once_per_user_and_keep_fallbacks(self):
        class HTTPException(Exception):
            pass

        resolve = bot_method('resolve_roster_names', discord=SimpleNamespace(HTTPException=HTTPException))
        guild = SimpleNamespace(get_member=Mock(side_effect=lambda uid: (
            SimpleNamespace(display_name='캐시 닉네임') if uid == 1 else None)),
            fetch_member=AsyncMock(side_effect=[SimpleNamespace(display_name='서버 닉네임'), HTTPException()]))
        roster = {0: [('1', 'global 1'), ('2', 'global 2'), ('3', 'old 3')],
                  1: [('2', 'old 2'), ('3', 'old 3')]}
        result = await resolve(None, guild, roster)
        self.assertEqual(dict(result[0]), {'1': '캐시 닉네임', '2': '서버 닉네임', '3': 'old 3'})
        self.assertEqual(dict(result[1]), {'2': '서버 닉네임', '3': 'old 3'})
        self.assertEqual(guild.fetch_member.await_count, 2)
        self.assertEqual(guild.get_member.call_count, 3)
        self.assertEqual(roster[0][1], ('2', 'global 2'))
        self.assertEqual(result[6], [])

    async def test_live_roster_resolves_old_and_new_signups_together(self):
        from core.storage import merge_rosters
        import json
        live = bot_method('live_roster', json=json, merge_rosters=merge_rosters)

        async def users(*, limit):
            yield SimpleNamespace(id=11, bot=False, display_name='global')
            yield SimpleNamespace(id=99, bot=True, display_name='bot')

        reaction = SimpleNamespace(emoji=SimpleNamespace(id=42), users=users)
        guild = object()
        channel = SimpleNamespace(guild=guild, fetch_message=AsyncMock(
            return_value=SimpleNamespace(reactions=[reaction])))
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=channel),
                              store=SimpleNamespace(final_roster=Mock(return_value={0: [('10', 'saved')]})),
                              resolve_roster_names=AsyncMock(return_value='resolved'))
        rec = {'thread_id': '100', 'message_id': '101', 'emoji_ids_json': '{"0":"42"}',
               'reopened_with_snapshot': True}
        self.assertEqual(await live(bot, rec), 'resolved')
        passed_guild, roster = bot.resolve_roster_names.await_args.args
        self.assertIs(passed_guild, guild)
        self.assertEqual(dict(roster[0]), {'10': 'saved', '11': 'global'})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Storage(Path(self.tmp.name) / 'test.sqlite3')
        self.addCleanup(self.store.db.close)
        self.store.reserve_creation(START, 'glenn', 50, NOW)
        self.bot = SimpleNamespace(store=self.store, user=SimpleNamespace(id=7))
        self.find = bot_method('find_unrecorded_thread', datetime=datetime, title=title,
                               legacy_thread_title=legacy_thread_title)

    def thread(self, uid, name=None, created=NOW, owner=7, parent=50):
        return SimpleNamespace(id=uid, name=name or title(START, 'glenn'), created_at=created,
                               owner_id=owner, parent_id=parent)

    def parent(self, active, archived=()):
        async def archived_threads(*, limit):
            for thread in archived:
                yield thread
        return SimpleNamespace(id=50, guild=SimpleNamespace(active_threads=AsyncMock(return_value=active)),
                               archived_threads=archived_threads)

    async def test_recovery_skips_old_foreign_and_recorded_threads(self):
        self.store.insert(10, START, 100, None, {}, '4f')
        expected = self.thread(15)
        parent = self.parent([self.thread(10), self.thread(11, created=NOW-timedelta(days=365)),
                              self.thread(12, owner=8), self.thread(13, parent=51)], [expected])
        self.assertIs(await self.find(self.bot, parent, START, 'glenn'), expected)

    async def test_recovery_recognizes_previous_dungeon_title(self):
        expected = self.thread(15, name=legacy_thread_title(START, 'glenn'))
        parent = self.parent([expected, self.thread(16, name=legacy_thread_title(START, '4f'))])
        self.assertIs(await self.find(self.bot, parent, START, 'glenn'), expected)

    async def test_recovery_refuses_ambiguous_shared_parent(self):
        self.store.reserve_creation(START, '4f', 50, NOW)
        with self.assertRaises(RuntimeError):
            await self.find(self.bot, self.parent([self.thread(15)]), START, 'glenn')

    async def test_recovery_ignores_other_dungeon_reserved_thread(self):
        self.store.reserve_creation(START, '4f', 50, NOW)
        self.store.record_created_thread(START, '4f', 10)
        expected = self.thread(15)
        self.assertIs(await self.find(self.bot, self.parent([self.thread(10), expected]), START, 'glenn'), expected)


if __name__ == '__main__':
    unittest.main()
