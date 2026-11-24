"""Offline regression tests for persistent Glenn settings and the 4F survey."""
import ast
import asyncio
import re
import sqlite3
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.calendar import (FOURF_RUN_EMOJIS, FOURF_PREFERENCE_CONTENT, KST,
                           starter_content, is_starter_message)
from core.storage import Storage, merge_run_preferences
from test_roster_and_recovery import bot_method

START = date(2026, 11, 26)
NOW = datetime(2026, 11, 24, 12, tzinfo=KST)
EMOJIS = {str(i): str(1000+i) for i in range(7)}
ROSTER = {0: [('1', '가람'), ('2', '나래')]}
PREFS = {3: [('1', '가람'), ('2', '나래')], 4: [('1', '가람')], 5: []}
REOPEN_NOTE = '\n\n🔓 재모집 중 — 기존 신청자는 다시 누르지 않아도 됩니다.'
PREFERENCE_REOPEN_NOTE = '\n\n🔓 기존 희망 횟수는 유지됩니다. 변경하려면 새로 반응해 주세요.'
CLOSED_PREFIX = '🔒 모집 마감\n'
DISCORD = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.store = Storage(self.path)
        self.addCleanup(self.store.db.close)

    def test_glenn_settings_persist_and_partial_updates(self):
        self.assertEqual(self.store.glenn_settings(), (5, ''))
        self.assertEqual(self.store.update_glenn_settings(run_count=7, memo='이벤트 상점'), (7, '이벤트 상점'))
        self.assertEqual(self.store.update_glenn_settings(run_count=4), (4, '이벤트 상점'))
        self.assertEqual(self.store.update_glenn_settings(memo='추가 입장권'), (4, '추가 입장권'))
        second = Storage(self.path)
        try:
            self.assertEqual(second.glenn_settings(), (4, '추가 입장권'))
            self.assertEqual(second.update_glenn_settings(clear_memo=True), (4, ''))
            self.assertEqual(self.store.glenn_settings(), (4, ''))
        finally:
            second.db.close()

    def test_invalid_settings_are_not_saved(self):
        self.store.update_glenn_settings(run_count=6, memo='메모')
        for kwargs in ({'run_count': 0}, {'run_count': 100}, {'run_count': True},
                       {'memo': 'a'*1001}, {'memo': 'abc', 'clear_memo': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.update_glenn_settings(**kwargs)
        self.assertEqual(self.store.glenn_settings(), (6, '메모'))

    def test_only_glenn_message_includes_persistent_settings(self):
        self.assertIn('이번주 글렌 횟수 5릴', starter_content(START, None, 'glenn'))
        text = starter_content(START, None, 'glenn', role_id=333,
                               glenn_runs=7, glenn_memo='추가 입장권')
        self.assertIn('이번주 글렌 횟수 7릴', text)
        self.assertIn('추가 입장권', text)
        self.assertTrue(is_starter_message(text, START, 'glenn'))
        self.assertNotIn('이번주 글렌 횟수', starter_content(START, None, '4f', glenn_runs=7, glenn_memo='추가 입장권'))
        self.assertNotIn('추가 입장권', starter_content(START, None, '4f', glenn_memo='추가 입장권'))

    def test_preference_highest_then_explicit_reopen_override(self):
        initial = merge_run_preferences({}, PREFS)
        self.assertEqual(initial, {3: [('2', '나래')], 4: [('1', '가람')], 5: []})
        result = merge_run_preferences(initial, {3: [('1', '새 이름')], 5: [('3', '다온')]})
        self.assertEqual(dict(result[3]), {'1': '새 이름', '2': '나래'})
        self.assertEqual(result[4], [])
        self.assertEqual(result[5], [('3', '다온')])

    def test_preferences_saved_with_roster_and_kept_across_restart_and_reopen(self):
        self.store.insert(10, START, 11, None, EMOJIS, '4f', preference_message_id=12)
        with self.assertRaises(ValueError):
            self.store.finalize(10, ROSTER, NOW)  # no silent loss of survey
        self.assertEqual(self.store.by_thread(10)['status'], 'open')
        self.store.finalize(10, ROSTER, NOW, run_preferences=PREFS)
        self.assertEqual(self.store.final_roster(10)[0], ROSTER[0])
        self.assertEqual(self.store.final_run_preferences(10)[4], [('1', '가람')])
        second = Storage(self.path)
        try:
            second.reopen(10, None, EMOJIS)
            self.assertEqual(second.by_thread(10)['preference_message_id'], '12')
            prefs = merge_run_preferences(second.final_run_preferences(10), {5: [('1', '가람')]})
            second.finalize(10, ROSTER, NOW, run_preferences=prefs)
            self.assertEqual(second.final_run_preferences(10), {3: [('2', '나래')], 4: [], 5: [('1', '가람')]})
        finally:
            second.db.close()

    def test_snapshot_transaction_rolls_back_both_lists_on_error(self):
        self.store.insert(10, START, 11, None, EMOJIS, '4f', preference_message_id=12)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finalize(10, {0: [('1', None)]}, NOW, run_preferences=PREFS)
        self.assertEqual(self.store.by_thread(10)['status'], 'open')
        self.assertEqual(self.store.final_run_preferences(10), {3: [], 4: [], 5: []})

    def test_deletion_removes_only_target_preferences_and_not_glenn_settings(self):
        self.store.update_glenn_settings(run_count=6, memo='keep')
        for thread, start in [(10, START), (20, START + timedelta(days=7))]:
            self.store.insert(thread, start, thread+1, None, EMOJIS, '4f', preference_message_id=thread+2)
            self.store.finalize(thread, ROSTER, NOW, run_preferences=PREFS)
        self.store.delete_recruitment(START, '4f', NOW, expected_record=self.store.by_thread(10), expected_reservation=None)
        self.assertEqual(self.store.final_run_preferences(10), {3: [], 4: [], 5: []})
        self.assertEqual(self.store.final_run_preferences(20)[4], [('1', '가람')])
        self.assertEqual(self.store.glenn_settings(), (6, 'keep'))

    def test_creation_reservation_retains_survey_id(self):
        self.store.reserve_creation(START, '4f', 7, NOW)
        self.store.record_created_thread(START, '4f', 10)
        self.store.record_recruitment_message(START, '4f', 11)
        self.store.record_preference_message(START, 12)
        second = Storage(self.path)
        try:
            self.assertEqual(second.creation_reservation(START, '4f')['preference_message_id'], '12')
            second.record_preference_message(START, 12)
            with self.assertRaises(RuntimeError):
                second.record_preference_message(START, 99)
        finally:
            second.db.close()

    def test_upgrade_old_rows_without_survey(self):
        # Make a real pre-feature database without requiring ALTER DROP COLUMN.
        old_path = Path(self.tmp.name) / 'old.sqlite3'
        raw = sqlite3.connect(old_path)
        raw.executescript("""
            CREATE TABLE recruitments (
              thread_id TEXT PRIMARY KEY, raid_type TEXT NOT NULL,
              week_start TEXT NOT NULL, week_end TEXT NOT NULL, message_id TEXT NOT NULL,
              deadline_at TEXT NOT NULL, emoji_ids_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open', closed_at TEXT,
              reactions_cleared INTEGER NOT NULL DEFAULT 0,
              reopened_with_snapshot INTEGER NOT NULL DEFAULT 0,
              UNIQUE(raid_type,week_start));
            CREATE TABLE creation_reservations (
              raid_type TEXT NOT NULL, week_start TEXT NOT NULL, parent_channel_id TEXT NOT NULL,
              thread_id TEXT, message_id TEXT, created_at TEXT NOT NULL,
              PRIMARY KEY(raid_type,week_start));
            CREATE TABLE final_roster (
              thread_id TEXT NOT NULL, day_index INTEGER NOT NULL, user_id TEXT NOT NULL,
              display_name TEXT NOT NULL, PRIMARY KEY(thread_id,day_index,user_id));
            INSERT INTO recruitments VALUES ('10','4f','2026-11-26','2026-12-02','11','','{}',
                                             'closed','2026-11-24T12:00:00+09:00',1,0);
        """)
        raw.executemany('INSERT INTO final_roster VALUES (?, ?, ?, ?)',
                        [('10', 0, uid, name) for uid,name in ROSTER[0]])
        raw.commit()
        raw.close()
        second = Storage(old_path)
        try:
            self.assertIsNone(second.by_thread(10)['preference_message_id'])
            self.assertEqual(second.final_roster(10)[0], ROSTER[0])
            self.assertEqual(second.glenn_settings(), (5, ''))
        finally:
            second.db.close()


class DiscordFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Storage(Path(self.tmp.name) / 'db.sqlite3')
        self.addCleanup(self.store.db.close)
        self.store.insert(10, START, 11, None, EMOJIS, '4f', preference_message_id=12)
        self.day = SimpleNamespace(id=11, content=starter_content(START, None, '4f'), reactions=[],
                                   clear_reactions=AsyncMock(), edit=AsyncMock(), add_reaction=AsyncMock())
        self.survey = SimpleNamespace(id=12, content=FOURF_PREFERENCE_CONTENT, reactions=[],
                                      clear_reactions=AsyncMock(), edit=AsyncMock(), add_reaction=AsyncMock())
        self.channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=lambda mid: {11:self.day, 12:self.survey}[mid]),
                                       guild=object())
        self.bot = SimpleNamespace(store=self.store, op_lock=asyncio.Lock(),
                                   fetch_channel=AsyncMock(return_value=self.channel),
                                   live_roster=AsyncMock(return_value=ROSTER),
                                   live_run_preferences=AsyncMock(return_value=PREFS),
                                   get_emojis=AsyncMock(return_value=[SimpleNamespace(id=int(v)) for v in EMOJIS.values()]))
        self.finish = bot_method('finish', datetime=datetime, KST=KST, discord=DISCORD,
                                 REOPEN_NOTE=REOPEN_NOTE, PREFERENCE_REOPEN_NOTE=PREFERENCE_REOPEN_NOTE, CLOSED_PREFIX=CLOSED_PREFIX)

    async def test_close_saves_both_before_clearing_and_retries_cleanup(self):
        async def verify_snapshot():
            self.assertEqual(self.store.by_thread(10)['status'], 'closed')
            self.assertEqual(self.store.final_run_preferences(10)[4], [('1', '가람')])
        self.day.clear_reactions.side_effect = verify_snapshot
        self.survey.clear_reactions.side_effect = RuntimeError('temporary failure')
        with self.assertRaises(RuntimeError):
            await self.finish(self.bot, self.store.by_thread(10))
        self.assertEqual(self.store.by_thread(10)['reactions_cleared'], 0)
        self.survey.clear_reactions.side_effect = verify_snapshot
        await self.finish(self.bot, self.store.by_thread(10))
        self.assertEqual(self.store.by_thread(10)['reactions_cleared'], 1)
        self.assertEqual(self.bot.live_run_preferences.await_count, 1)
        self.assertTrue(self.survey.edit.await_args.kwargs['content'].startswith(CLOSED_PREFIX))

    async def test_reopen_restores_both_and_preserves_snapshot(self):
        self.store.finalize(10, ROSTER, NOW, run_preferences=PREFS)
        self.day.content = CLOSED_PREFIX + self.day.content
        self.survey.content = CLOSED_PREFIX + self.survey.content
        namespace = {'datetime': datetime, 'KST': KST, 're': re, 'CLOSED_PREFIX': CLOSED_PREFIX,
                     'REOPEN_NOTE': REOPEN_NOTE}
        tree = ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'reopened_starter_content')
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<helper>', 'exec'), namespace)
        reopen = bot_method('reopen_recruitment', datetime=datetime, KST=KST, AUTO_CLOSE=False,
                            discord=DISCORD, FOURF_RUN_EMOJIS=FOURF_RUN_EMOJIS,
                            FOURF_PREFERENCE_CONTENT=FOURF_PREFERENCE_CONTENT,
                            PREFERENCE_REOPEN_NOTE=PREFERENCE_REOPEN_NOTE,
                            reopened_starter_content=namespace['reopened_starter_content'])
        rec, changed = await reopen(self.bot, 10)
        self.assertTrue(changed)
        self.assertEqual(rec['status'], 'open')
        self.assertEqual(rec['reopened_with_snapshot'], 1)
        self.assertEqual(self.survey.add_reaction.await_count, 3)
        self.assertEqual(self.day.add_reaction.await_count, 7)
        self.assertEqual(self.store.final_run_preferences(10)[4], [('1', '가람')])
        self.assertIn(PREFERENCE_REOPEN_NOTE, self.survey.edit.await_args.kwargs['content'])
        old_glenn = starter_content(START, None, 'glenn', glenn_runs=7, glenn_memo='예전 입장권')
        text = namespace['reopened_starter_content'](CLOSED_PREFIX+old_glenn, None)
        self.assertEqual(text, old_glenn + REOPEN_NOTE)

    async def test_live_preference_reactions_deduplicate_and_resolve_names(self):
        async def users3(*, limit):
            yield SimpleNamespace(id=1, bot=False, display_name='global')
            yield SimpleNamespace(id=99, bot=True, display_name='bot')
        async def users4(*, limit):
            yield SimpleNamespace(id=1, bot=False, display_name='global')
        async def users5(*, limit):
            yield SimpleNamespace(id=2, bot=False, display_name='global2')
        self.survey.reactions = [SimpleNamespace(emoji='3️⃣', users=users3),
                                 SimpleNamespace(emoji='4⃣', users=users4),
                                 SimpleNamespace(emoji='5️⃣', users=users5)]
        self.bot.resolve_roster_names = AsyncMock(side_effect=lambda guild, roster: {
            k: [(uid, '서버 닉네임 '+uid) for uid, _name in v] for k,v in roster.items()})
        live = bot_method('live_run_preferences', FOURF_RUN_EMOJIS=FOURF_RUN_EMOJIS, merge_run_preferences=merge_run_preferences)
        result = await live(self.bot, self.store.by_thread(10))
        self.assertEqual(result, {3: [], 4: [('1', '서버 닉네임 1')], 5: [('2', '서버 닉네임 2')]})

    async def test_preference_message_created_and_seeded(self):
        next_start = START + timedelta(days=7)
        self.store.reserve_creation(next_start, '4f', 7, NOW)
        async def history(**kwargs):
            if False:
                yield None
        self.channel.history = history
        self.channel.send = AsyncMock(return_value=self.survey)
        self.bot.user = SimpleNamespace(id=99)
        seed = bot_method('seed_run_reactions', FOURF_RUN_EMOJIS=FOURF_RUN_EMOJIS)
        self.bot.seed_run_reactions = lambda msg: seed(self.bot, msg)
        ensure = bot_method('ensure_preference_message', FOURF_PREFERENCE_CONTENT=FOURF_PREFERENCE_CONTENT, discord=DISCORD)
        await ensure(self.bot, self.channel, next_start)
        self.channel.send.assert_awaited_once_with(FOURF_PREFERENCE_CONTENT, allowed_mentions=None)
        self.assertEqual([call.args[0] for call in self.survey.add_reaction.await_args_list], list(FOURF_RUN_EMOJIS.values()))
        self.assertEqual(self.store.creation_reservation(next_start, '4f')['preference_message_id'], '12')

    async def test_preference_creation_reuses_unrecorded_message(self):
        self.store.reserve_creation(START + timedelta(days=7), '4f', 7, NOW)
        self.survey.author = SimpleNamespace(id=99)
        async def history(**kwargs):
            yield self.survey
        self.channel.history = history
        self.channel.send = AsyncMock()
        self.bot.user = SimpleNamespace(id=99)
        self.bot.seed_run_reactions = AsyncMock()
        ensure = bot_method('ensure_preference_message', FOURF_PREFERENCE_CONTENT=FOURF_PREFERENCE_CONTENT, discord=DISCORD)
        await ensure(self.bot, self.channel, START + timedelta(days=7))
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.creation_reservation(START + timedelta(days=7), '4f')['preference_message_id'], '12')
        await ensure(self.bot, self.channel, START + timedelta(days=7), message_id='12')
        self.channel.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
