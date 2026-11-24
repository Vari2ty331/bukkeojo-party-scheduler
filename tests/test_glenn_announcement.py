"""Glenn settings can update the exact existing recruitment without reopening it."""
import asyncio
import logging
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.calendar import (KST, starter_content, update_glenn_starter,
                           glenn_announcement_week, recruitment_week_start)
from core.storage import Storage
from test_roster_and_recovery import bot_method

START = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, 8, tzinfo=KST)
CLOSED = '🔒 모집 마감\n'
REOPEN = '\n\n🔓 재모집 중 — 기존 신청자는 다시 누르지 않아도 됩니다.'
DISCORD = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: 'no-mentions'), Interaction=object)


class AnnouncementTextTests(unittest.TestCase):
    def test_open_closed_and_reopened_preserve_every_other_line(self):
        deadline = NOW + timedelta(hours=5)
        for prefix, suffix in (('', ''), (CLOSED, ''), ('', REOPEN)):
            with self.subTest(prefix=prefix, suffix=suffix):
                old = prefix + starter_content(START, deadline, 'glenn', role_id=42,
                                               glenn_runs=5, glenn_memo='기존 안내\n두 번째 줄') + suffix
                expected = prefix + starter_content(START, deadline, 'glenn', role_id=42,
                                                    glenn_runs=7, glenn_memo='새 안내') + suffix
                self.assertEqual(update_glenn_starter(old, START, 7, '새 안내'), expected)
                self.assertEqual(update_glenn_starter(expected, START, 7, '새 안내'), expected)
                cleared = update_glenn_starter(expected, START, 7, '')
                self.assertEqual(cleared, prefix + starter_content(START, deadline, 'glenn', role_id=42,
                                                                  glenn_runs=7) + suffix)

    def test_add_settings_to_pre_upgrade_post(self):
        original = starter_content(START, None, 'glenn').replace('\n\n**이번주 글렌 횟수 5릴**', '')
        self.assertEqual(update_glenn_starter(original, START, 4, '티켓'),
                         starter_content(START, None, 'glenn', glenn_runs=4, glenn_memo='티켓'))

    def test_never_modify_wrong_week_or_dungeon_or_unknown_content(self):
        for content in (starter_content(START, None, '4f'),
                        starter_content(START + timedelta(days=7), None, 'glenn'),
                        'unrecognized', starter_content(START, None, 'glenn').split('\n\n')[0]):
            with self.subTest(content=content), self.assertRaises(ValueError):
                update_glenn_starter(content, START, 7, 'memo')


class AnnouncementCycleTests(unittest.TestCase):
    def test_wednesday_opening_and_thursday_reset_are_separate(self):
        cases = [(datetime(2026, 11, 25, 10, 59, tzinfo=KST), date(2026, 11, 19)),
                 (datetime(2026, 11, 25, 11, 0, tzinfo=KST), START),
                 (datetime(2026, 11, 26, 6, 59, tzinfo=KST), START),
                 (datetime(2026, 11, 26, 7, 0, tzinfo=KST), START),
                 (datetime(2026, 12, 2, 10, 59, tzinfo=KST), START),
                 (datetime(2026, 12, 2, 11, 0, tzinfo=KST), date(2026, 12, 3))]
        for dt, expected in cases:
            with self.subTest(dt=dt):
                self.assertEqual(glenn_announcement_week(dt), expected)
                self.assertEqual(glenn_announcement_week(dt.astimezone(timezone.utc)), expected)
        self.assertEqual(recruitment_week_start(NOW), date(2026, 12, 3))

    def test_cycle_handles_year_boundary(self):
        self.assertEqual(glenn_announcement_week(datetime(2026, 12, 31, 8, tzinfo=KST)),
                         date(2026, 12, 31))
        self.assertEqual(glenn_announcement_week(datetime(2027, 1, 6, 11, tzinfo=KST)),
                         date(2027, 1, 7))


class EditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Storage(Path(tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.db.close)
        emojis = {str(i): str(1000+i) for i in range(7)}
        self.store.insert(10, START, 11, None, emojis, 'glenn')
        self.store.insert(20, START + timedelta(days=7), 21, None, emojis, 'glenn')
        self.store.insert(30, START, 31, None, emojis, '4f')
        self.store.finalize(10, {0: [('1', '가람')]}, NOW)
        self.store.mark_reactions_cleared(10)
        self.msg = SimpleNamespace(content=CLOSED + starter_content(START, None, 'glenn', role_id=42),
                                   author=SimpleNamespace(id=7), edit=AsyncMock(),
                                   clear_reactions=AsyncMock(), add_reaction=AsyncMock(), attachments=['PNG'])
        self.thread = SimpleNamespace(fetch_message=AsyncMock(return_value=self.msg))
        self.bot = SimpleNamespace(store=self.store, op_lock=asyncio.Lock(),
                                   user=SimpleNamespace(id=7),
                                   fetch_channel=AsyncMock(return_value=self.thread))
        fn = bot_method('edit_glenn_announcement', date=date, discord=DISCORD,
                        update_glenn_starter=update_glenn_starter)
        self.bot.edit_glenn_announcement = lambda rec, count, memo: fn(self.bot, rec, count, memo)
        # Extract the real command body, with decorators removed, to exercise it
        # without installing discord.py or using a live token.
        import ast
        source = Path(__file__).resolve().parents[1] / 'bot.py'
        node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'glenn_settings')
        node.decorator_list = []
        self.clock = SimpleNamespace(now=lambda tz: NOW.astimezone(tz))
        ns = {'discord': DISCORD, 'bot': self.bot, 'log': logging.getLogger('test'),
              'require_organizer': AsyncMock(return_value=True), 'datetime': self.clock,
              'KST': KST, 'glenn_announcement_week': glenn_announcement_week}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), ns)
        self.command = ns['glenn_settings']

    def interaction(self, thread_id):
        return SimpleNamespace(channel_id=thread_id, response=SimpleNamespace(defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_closed_thread_update_uses_invoking_id_not_future_week(self):
        before = dict(self.store.by_thread(10))
        future = dict(self.store.by_thread(20))
        i = self.interaction(10)
        await self.command(i, run_count=7, memo='추가 티켓')
        self.assertEqual(self.store.glenn_settings(), (7, '추가 티켓'))
        self.bot.fetch_channel.assert_awaited_once_with(10)
        self.thread.fetch_message.assert_awaited_once_with(11)
        self.msg.edit.assert_awaited_once_with(
            content=CLOSED + starter_content(START, None, 'glenn', role_id=42,
                                             glenn_runs=7, glenn_memo='추가 티켓'),
            allowed_mentions='no-mentions')
        self.msg.clear_reactions.assert_not_awaited()
        self.msg.add_reaction.assert_not_awaited()
        self.assertEqual(dict(self.store.by_thread(10)), before)
        self.assertEqual(dict(self.store.by_thread(20)), future)
        self.assertEqual(self.store.final_roster(10)[0], [('1', '가람')])
        self.assertIn('현재 글렌 모집 글에도 반영했습니다', i.followup.send.await_args.args[0])

    async def test_lookup_only_does_not_edit(self):
        i = self.interaction(10)
        await self.command(i)
        self.assertEqual(self.store.glenn_settings(), (5, ''))
        self.bot.fetch_channel.assert_not_awaited()
        self.assertIn('현재 설정입니다', i.followup.send.await_args.args[0])

    async def test_update_outside_glenn_thread_targets_current_cycle(self):
        for channel_id in (30, 99):
            with self.subTest(channel_id=channel_id):
                self.bot.fetch_channel.reset_mock()
                self.msg.edit.reset_mock()
                await self.command(self.interaction(channel_id), run_count=6)
                self.assertEqual(self.store.glenn_settings(), (6, ''))
                self.bot.fetch_channel.assert_awaited_once_with(10)
                self.assertIn('6릴', self.msg.edit.await_args.kwargs['content'])

    async def test_wednesday_opening_switches_default_target(self):
        self.clock.now = lambda tz: datetime(2026, 12, 2, 11, tzinfo=KST).astimezone(tz)
        self.msg.content = starter_content(START + timedelta(days=7), None, 'glenn')
        await self.command(self.interaction(99), memo='새 안내')
        self.bot.fetch_channel.assert_awaited_once_with(20)
        self.thread.fetch_message.assert_awaited_once_with(21)
        self.assertIn('새 안내', self.msg.edit.await_args.kwargs['content'])

    async def test_no_current_thread_saves_without_edit_or_creation(self):
        self.clock.now = lambda tz: datetime(2026, 12, 9, 11, tzinfo=KST).astimezone(tz)
        i = self.interaction(99)
        await self.command(i, run_count=4)
        self.assertEqual(self.store.glenn_settings(), (4, ''))
        self.bot.fetch_channel.assert_not_awaited()
        self.assertIn('아직 없습니다', i.followup.send.await_args.args[0])

    async def test_open_thread_memo_clear_updates_without_resetting_runs(self):
        self.store.update_glenn_settings(run_count=7, memo='기존 메모')
        self.msg.content = starter_content(START + timedelta(days=7), None, 'glenn',
                                           glenn_runs=7, glenn_memo='기존 메모')
        before = dict(self.store.by_thread(20))
        await self.command(self.interaction(20), clear_memo=True)
        self.assertEqual(self.store.glenn_settings(), (7, ''))
        self.bot.fetch_channel.assert_awaited_once_with(20)
        self.assertEqual(self.msg.edit.await_args.kwargs['content'],
                         starter_content(START + timedelta(days=7), None, 'glenn', glenn_runs=7))
        self.assertEqual(dict(self.store.by_thread(20)), before)
        self.msg.clear_reactions.assert_not_awaited()

    async def test_failed_edit_can_retry_using_saved_settings(self):
        self.msg.edit.side_effect = RuntimeError('mock network failure')
        i = self.interaction(10)
        with self.assertLogs('test', level='ERROR'):
            await self.command(i, run_count=7, memo='티켓')
        self.assertEqual(self.store.glenn_settings(), (7, '티켓'))
        self.assertIn('수정에 실패했습니다', i.followup.send.await_args.args[0])
        self.msg.edit.side_effect = None
        await self.command(self.interaction(10), run_count=7)
        self.assertIn('7릴', self.msg.edit.await_args.kwargs['content'])
        self.assertIn('티켓', self.msg.edit.await_args.kwargs['content'])

    async def test_refuse_non_bot_message(self):
        self.msg.author.id = 999
        with self.assertRaises(ValueError):
            await self.bot.edit_glenn_announcement(self.store.by_thread(10), 7, '')
        self.msg.edit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
