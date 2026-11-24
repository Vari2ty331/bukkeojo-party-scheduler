import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.calendar import (KST, is_starter_message, next_thursday, raid_label,
                           schedule_for_week, starter_content, title, legacy_thread_title, week_days,
                           opening_for_week, due_openings, recruitment_week_start)
from core.storage import Storage, merge_rosters


START = date(2026, 11, 26)
DEADLINE = datetime(2026, 11, 25, 23, tzinfo=KST)
EMOJIS = {str(i): str(i + 200) for i in range(7)}


class CalendarTests(unittest.TestCase):
    def test_thursday_to_wednesday_month_boundary(self):
        days = week_days(START)
        self.assertEqual(days[-1], date(2026, 12, 2))
        self.assertEqual([d.weekday() for d in days], [3, 4, 5, 6, 0, 1, 2])

    def test_next_thursday_strict(self):
        self.assertEqual(next_thursday(date(2026, 11, 24)), START)
        self.assertEqual(next_thursday(START), date(2026, 12, 3))

    def test_recruitment_week_rolls_over_at_seven_kst(self):
        following = date(2026, 12, 3)
        cases = [
            (datetime(2026, 11, 25, 23, 59, tzinfo=KST), START),
            (datetime(2026, 11, 26, 0, tzinfo=KST), START),
            (datetime(2026, 11, 26, 6, 59, 59, 999999, tzinfo=KST), START),
            (datetime(2026, 11, 26, 7, tzinfo=KST), following),
            (datetime(2026, 11, 26, 7, 0, 1, tzinfo=KST), following),
            (datetime(2026, 11, 27, 12, tzinfo=KST), following),
            (datetime(2026, 12, 3, 6, 59, tzinfo=KST), following),
            (datetime(2026, 12, 3, 7, tzinfo=KST), date(2026, 12, 10)),
            (datetime(2026, 12, 31, 7, tzinfo=KST), date(2027, 1, 7)),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(recruitment_week_start(now), expected)

    def test_recruitment_week_is_independent_of_server_timezone(self):
        self.assertEqual(recruitment_week_start(datetime(2026, 11, 25, 21, 59, tzinfo=timezone.utc)), START)
        self.assertEqual(recruitment_week_start(datetime(2026, 11, 25, 22, tzinfo=timezone.utc)), date(2026, 12, 3))
        with self.assertRaises(ValueError):
            recruitment_week_start(datetime(2026, 11, 26, 7))

    def test_schedule(self):
        op, cl = schedule_for_week(START, 1, '18:00', 1, '23:00')
        self.assertEqual(op, datetime(2026, 11, 25, 18, tzinfo=KST))
        self.assertEqual(cl, DEADLINE)

    def test_independent_openings(self):
        self.assertEqual(opening_for_week(START, '4f'), datetime(2026, 11, 23, 11, tzinfo=KST))
        self.assertEqual(opening_for_week(START, 'glenn'), datetime(2026, 11, 25, 11, tzinfo=KST))
        cases = [
            (datetime(2026, 11, 23, 10, 59, tzinfo=KST), []),
            (datetime(2026, 11, 23, 11, tzinfo=KST), [(START, '4f')]),
            (datetime(2026, 11, 25, 10, 59, tzinfo=KST), [(START, '4f')]),
            (datetime(2026, 11, 25, 11, tzinfo=KST), [(START, 'glenn'), (START, '4f')]),
            (datetime(2026, 11, 25, 23, 59, tzinfo=KST), [(START, 'glenn'), (START, '4f')]),
            (datetime(2026, 11, 26, 0, tzinfo=KST), [(START, 'glenn'), (START, '4f')]),
            (datetime(2026, 11, 26, 6, 59, tzinfo=KST), [(START, 'glenn'), (START, '4f')]),
            (datetime(2026, 11, 26, 7, tzinfo=KST), []),
            (datetime(2026, 11, 30, 11, tzinfo=KST), [(date(2026, 12, 3), '4f')]),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(due_openings(now), expected)

    def test_manual_only_starter(self):
        text = starter_content(START, None, '4f')
        self.assertEqual(text, '**4관 파티 모집 | 11/26 ~ 12/02**\n\n'
                         '참여 가능한 날의 **요일 이모지**를 모두 눌러 주세요.\n'
                         '시간 제한이 있다면 이 스레드에 댓글로 남겨 주세요.')
        self.assertNotIn('신청 마감', text)
        self.assertNotIn('⏰', text)
        self.assertTrue(is_starter_message(text, START, '4f'))

    def test_glenn_only_departure_note(self):
        note = '평일은 저녁, 주말/공휴일은 점심부터 출발'
        self.assertEqual(starter_content(START, None, 'glenn').count(note), 1)
        self.assertNotIn(note, starter_content(START, None, '4f'))
        self.assertTrue(is_starter_message(starter_content(START, None, 'glenn'), START, 'glenn'))

    def test_starter_mentions_only_configured_role_when_supplied(self):
        glenn = starter_content(START, DEADLINE, 'glenn', role_id=111222333)
        fourf = starter_content(START, DEADLINE, '4f', role_id=444555666)
        self.assertTrue(glenn.startswith('<@&111222333>\n'))
        self.assertNotIn('<@&444555666>', glenn)
        self.assertTrue(fourf.startswith('<@&444555666>\n'))
        self.assertNotIn('<@&111222333>', fourf)
        self.assertIn('11/26 ~ 12/02', glenn)
        self.assertNotIn('11/26(목)', glenn)
        self.assertNotIn('12/02(수)', glenn)
        self.assertIn('⏰ 신청 마감: **11/25 23:00 (한국 시간)**', glenn)
        # Historical posts without role mentions remain unchanged.
        self.assertFalse(starter_content(START, DEADLINE, 'glenn').startswith('<@&'))

    def test_starter_recognition_survives_role_change_and_deadline_edit(self):
        old_starter = starter_content(START, DEADLINE, '4f', role_id=123456789)
        self.assertTrue(is_starter_message(old_starter, START, '4f'))
        self.assertTrue(is_starter_message('🔒 모집 마감\n' + old_starter, START, '4f'))
        self.assertFalse(is_starter_message(old_starter, START, 'glenn'))
        self.assertFalse(is_starter_message(old_starter, START + timedelta(days=7), '4f'))

    def test_previous_calendar_starter_is_still_recognized(self):
        old = ('<@&123456789>\n**글렌 베르나 파티 모집 | 11/26 ~ 12/02**\n'
               '11/26(목) · 11/27(금) · 11/28(토) · 11/29(일) · '
               '11/30(월) · 12/01(화) · 12/02(수)\n\n'
               '참여 가능한 날의 **요일 이모지**를 모두 눌러 주세요.\n'
               '시간 제한이 있다면 이 스레드에 댓글로 남겨 주세요.\n'
               '신청 마감: 운영진이 직접 마감합니다.')
        self.assertTrue(is_starter_message(old, START, 'glenn'))
        self.assertFalse(is_starter_message(old, START, '4f'))
        self.assertFalse(is_starter_message(old, START + timedelta(days=7), 'glenn'))
        self.assertFalse(is_starter_message(old.replace('11/26 ~ 12/02', '11/26 ~ 12/03'), START, 'glenn'))

    def test_korean_starter_is_a_party_invitation(self):
        post = starter_content(START, DEADLINE, 'glenn')
        self.assertIn('글렌 베르나 파티 모집', post)
        self.assertIn('신청 마감', post)
        self.assertNotIn('레이드', post)
        self.assertTrue(is_starter_message(post, START, 'glenn'))

    def test_legacy_starter_is_still_recognized(self):
        old = '**글렌 베르나 — 이번 주 참여 가능 요일 조사**\n' + (
            '11/26(목) · 11/27(금) · 11/28(토) · 11/29(일) · 11/30(월) · 12/01(화) · 12/02(수)')
        self.assertTrue(is_starter_message(old, START, 'glenn'))

    def test_dungeon_labels_are_distinct(self):
        self.assertEqual(title(START, 'glenn'), '11.26 ~ 12.02')
        self.assertEqual(title(START, '4f'), '11.26 ~ 12.02')
        self.assertEqual(legacy_thread_title(START, 'glenn'), '11.26 ~ 12.02 글렌 베르나 모집')
        self.assertEqual(legacy_thread_title(START, '4f'), '11.26 ~ 12.02 4관 모집')
        self.assertIn('글렌 베르나', starter_content(START, DEADLINE, 'glenn'))
        self.assertIn('4관', starter_content(START, DEADLINE, '4f'))
        self.assertEqual(raid_label('legacy'), '기존 파티')


class StorageTests(unittest.TestCase):
    def test_manual_only_recruitment_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = Storage(path)
            db.insert('100', START, '101', None, EMOJIS, '4f')
            self.assertEqual(db.by_thread('100')['deadline_at'], '')
            db.finalize('100', {0: [('123', 'Alice')]}, DEADLINE)
            db.reopen('100', None, EMOJIS)
            self.assertEqual(db.by_thread('100')['deadline_at'], '')
            self.assertEqual(db.by_thread('100')['status'], 'open')
            self.assertEqual(db.final_roster('100')[0], [('123', 'Alice')])
            db.extend_reopened_deadline('100', None)
            self.assertEqual(db.by_thread('100')['deadline_at'], '')
            db.db.close()

    def test_two_recruitments_same_week_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = Storage(path)
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.insert('200', START, '201', DEADLINE, EMOJIS, '4f')
            self.assertEqual(db.by_week(START, 'glenn')['thread_id'], '100')
            self.assertEqual(db.by_week(START, '4f')['thread_id'], '200')
            self.assertEqual(len(db.open_recruitments()), 2)

            db.finalize('200', {0: [('123', 'Alice')], 1: [('456', 'Bob')]}, DEADLINE)
            self.assertEqual(db.by_thread('100')['status'], 'open')
            self.assertEqual(db.by_thread('200')['status'], 'closed')
            self.assertEqual(db.final_roster('200')[0], [('123', 'Alice')])
            self.assertEqual(db.final_roster('100')[0], [])
            db.mark_reactions_cleared('200')
            self.assertEqual(db.by_thread('200')['reactions_cleared'], 1)
            db.db.close()

            reopened = Storage(path)
            self.assertEqual(reopened.final_roster('200')[1], [('456', 'Bob')])
            self.assertEqual(reopened.by_week(START, 'glenn')['status'], 'open')
            reopened.db.close()

    def test_reopen_preserves_old_signups_and_merges_new_reactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'reopen.sqlite3'
            db = Storage(path)
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.insert('200', START, '201', DEADLINE, EMOJIS, '4f')
            original = {0: [('101', 'Alice')], 1: [('102', 'Bob')]}
            db.finalize('100', original, DEADLINE)
            db.mark_reactions_cleared('100')
            self.assertEqual(db.by_thread('100')['status'], 'closed')

            new_deadline = DEADLINE + timedelta(hours=2)
            updated_emojis = {str(i): str(i + 400) for i in range(7)}
            db.reopen('100', new_deadline, updated_emojis)
            extended_deadline = new_deadline + timedelta(hours=1)
            db.extend_reopened_deadline('100', extended_deadline)
            active = db.by_thread('100')
            self.assertEqual(active['status'], 'open')
            self.assertEqual(active['reopened_with_snapshot'], 1)
            self.assertEqual(active['reactions_cleared'], 0)
            self.assertEqual(active['deadline_at'], extended_deadline.isoformat())
            self.assertEqual(json.loads(active['emoji_ids_json']), updated_emojis)
            self.assertEqual(db.final_roster('100')[0], [('101', 'Alice')])
            self.assertEqual(db.by_thread('200')['status'], 'open')

            reactions = {0: [('101', 'Alice New'), ('103', 'Carol')], 2: [('104', 'David')]}
            combined = merge_rosters(db.final_roster('100'), reactions)
            self.assertEqual(combined[0], [('101', 'Alice New'), ('103', 'Carol')])
            self.assertEqual(combined[1], [('102', 'Bob')])
            self.assertEqual(combined[2], [('104', 'David')])
            db.finalize('100', combined, new_deadline)
            db.mark_reactions_cleared('100')
            self.assertEqual(db.by_thread('100')['reopened_with_snapshot'], 0)
            db.db.close()

            # A second reopen after reboot carries forward BOTH generations.
            db = Storage(path)
            self.assertEqual(db.final_roster('100')[0], combined[0])
            self.assertEqual(db.final_roster('100')[2], combined[2])
            db.reopen('100', new_deadline + timedelta(hours=1), updated_emojis)
            self.assertEqual(db.final_roster('100')[0], combined[0])
            db.db.close()

    def test_reopen_requires_existing_closed_recruitment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'x.sqlite3')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            with self.assertRaises(ValueError):
                db.reopen('100', DEADLINE, EMOJIS)
            with self.assertRaises(ValueError):
                db.reopen('missing', DEADLINE, EMOJIS)
            db.db.close()

    def test_v2_database_auto_adds_reopen_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v2.sqlite3'
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE recruitments (
                    thread_id TEXT PRIMARY KEY,
                    raid_type TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    week_end TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    emoji_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    closed_at TEXT,
                    reactions_cleared INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(raid_type, week_start)
                );
            """)
            old.commit()
            old.close()
            upgraded = Storage(path)
            upgraded.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            self.assertEqual(upgraded.by_thread('100')['reopened_with_snapshot'], 0)
            upgraded.db.close()

    def test_unique_within_each_dungeon_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'x.sqlite3')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            with self.assertRaises(sqlite3.IntegrityError):
                db.insert('102', START, '103', DEADLINE, EMOJIS, 'glenn')
            db.db.close()

    def test_creation_reservation_survives_restart_and_blocks_duplicate_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = Storage(path)
            self.assertTrue(db.reserve_creation(START, 'glenn', 999, DEADLINE))
            self.assertFalse(db.reserve_creation(START, 'glenn', 999, DEADLINE))
            # Two dungeons can be independently reserved for the same week.
            self.assertTrue(db.reserve_creation(START, '4f', 888, DEADLINE))
            db.db.close()

            resumed = Storage(path)
            job = resumed.creation_reservation(START, 'glenn')
            self.assertEqual(job['parent_channel_id'], '999')
            self.assertIsNone(job['thread_id'])
            resumed.record_created_thread(START, 'glenn', '123')
            resumed.record_recruitment_message(START, 'glenn', '124')
            job = resumed.creation_reservation(START, 'glenn')
            self.assertEqual(job['thread_id'], '123')
            self.assertEqual(job['message_id'], '124')
            with self.assertRaises(RuntimeError):
                resumed.record_created_thread(START, 'glenn', '9999')
            resumed.insert('123', START, '124', DEADLINE, EMOJIS, 'glenn')
            resumed.release_creation_reservation(START, 'glenn')
            self.assertIsNone(resumed.creation_reservation(START, 'glenn'))
            self.assertIsNotNone(resumed.creation_reservation(START, '4f'))
            self.assertFalse(resumed.reserve_creation(START, 'glenn', 999, DEADLINE))
            resumed.db.close()

    def test_deleted_open_thread_replacement_is_atomic_and_dungeon_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = Storage(path)
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.insert('200', START, '201', DEADLINE, EMOJIS, '4f')
            self.assertFalse(db.reserve_deleted_thread_replacement(START, 'glenn', '999', '500', DEADLINE))
            self.assertTrue(db.reserve_deleted_thread_replacement(START, 'glenn', '100', '500', DEADLINE))
            self.assertIsNone(db.by_thread('100'))
            self.assertEqual(db.by_thread('200')['status'], 'open')
            self.assertFalse(db.reserve_creation(START, 'glenn', '500', DEADLINE))
            db.db.close()
            db = Storage(path)
            self.assertIsNotNone(db.creation_reservation(START, 'glenn'))
            db.record_created_thread(START, 'glenn', '300')
            db.record_recruitment_message(START, 'glenn', '301')
            db.insert('300', START, '301', DEADLINE, EMOJIS, 'glenn')
            db.release_creation_reservation(START, 'glenn')
            self.assertEqual(db.by_week(START, 'glenn')['thread_id'], '300')
            db.db.close()

    def test_deleted_thread_replacement_preserves_closed_and_reopened_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'test.sqlite3')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.finalize('100', {0: [('7', 'Player')]}, DEADLINE)
            self.assertFalse(db.reserve_deleted_thread_replacement(START, 'glenn', '100', '500', DEADLINE))
            db.mark_reactions_cleared('100')
            db.reopen('100', DEADLINE + timedelta(hours=2), EMOJIS)
            self.assertFalse(db.reserve_deleted_thread_replacement(START, 'glenn', '100', '500', DEADLINE))
            self.assertEqual(db.final_roster('100')[0], [('7', 'Player')])
            db.db.close()

    def test_replacement_refuses_conflicting_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'test.sqlite3')
            db.reserve_creation(START, 'glenn', '500', DEADLINE)
            db.record_created_thread(START, 'glenn', '999')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            self.assertFalse(db.reserve_deleted_thread_replacement(START, 'glenn', '100', '500', DEADLINE))
            self.assertIsNotNone(db.by_thread('100'))
            self.assertEqual(db.creation_reservation(START, 'glenn')['thread_id'], '999')
            db.db.close()

    def test_explicit_delete_removes_only_target_week_and_dungeon(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite3'
            db = Storage(path)
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.insert('200', START, '201', DEADLINE, EMOJIS, '4f')
            db.insert('300', START + timedelta(days=7), '301', DEADLINE, EMOJIS, 'glenn')
            db.finalize('100', {0: [('7', 'Player')]}, DEADLINE)
            db.finalize('200', {1: [('8', 'Other')]}, DEADLINE)
            self.assertTrue(db.delete_recruitment(
                START, 'glenn', DEADLINE, expected_record=db.by_thread('100'), expected_reservation=None))
            self.assertIsNone(db.by_thread('100'))
            self.assertEqual(db.final_roster('100')[0], [])
            self.assertEqual(db.final_roster('200')[1], [('8', 'Other')])
            self.assertIsNotNone(db.by_thread('300'))
            self.assertTrue(db.is_cancelled(START, 'glenn'))
            self.assertFalse(db.is_cancelled(START, '4f'))
            self.assertFalse(db.is_cancelled(START + timedelta(days=7), 'glenn'))
            db.db.close()

            db = Storage(path)
            self.assertTrue(db.is_cancelled(START, 'glenn'))
            self.assertFalse(db.reserve_creation(START, 'glenn', '500', DEADLINE))
            self.assertTrue(db.reserve_creation(START, 'glenn', '500', DEADLINE, allow_cancelled=True))
            self.assertTrue(db.is_cancelled(START, 'glenn'))  # Not cleared until creation succeeds.
            db.record_created_thread(START, 'glenn', '400')
            db.record_recruitment_message(START, 'glenn', '401')
            db.insert('400', START, '401', DEADLINE, EMOJIS, 'glenn')
            db.release_creation_reservation(START, 'glenn')
            self.assertFalse(db.is_cancelled(START, 'glenn'))
            self.assertEqual(db.by_week(START, 'glenn')['thread_id'], '400')
            self.assertEqual(db.final_roster('400')[0], [])
            db.db.close()

    def test_explicit_delete_cleans_matching_creation_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'test.sqlite3')
            db.reserve_creation(START, 'glenn', '500', DEADLINE)
            db.record_created_thread(START, 'glenn', '100')
            db.record_recruitment_message(START, 'glenn', '101')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            self.assertTrue(db.delete_recruitment(
                START, 'glenn', DEADLINE, expected_record=db.by_thread('100'),
                expected_reservation=db.creation_reservation(START, 'glenn')))
            self.assertIsNone(db.creation_reservation(START, 'glenn'))
            self.assertIsNone(db.by_thread('100'))
            db.db.close()

    def test_explicit_delete_can_release_interrupted_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'test.sqlite3')
            db.reserve_creation(START, 'glenn', '500', DEADLINE)
            db.reserve_creation(START, '4f', '600', DEADLINE)
            self.assertTrue(db.delete_recruitment(
                START, 'glenn', DEADLINE, expected_record=None,
                expected_reservation=db.creation_reservation(START, 'glenn')))
            self.assertTrue(db.is_cancelled(START, 'glenn'))
            self.assertIsNone(db.creation_reservation(START, 'glenn'))
            self.assertIsNotNone(db.creation_reservation(START, '4f'))
            self.assertFalse(db.delete_recruitment(
                START, 'glenn', DEADLINE, expected_record=None, expected_reservation=None))
            db.db.close()

    def test_explicit_delete_rejects_changed_record_and_conflicting_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'test.sqlite3')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            old = db.by_thread('100')
            db.finalize('100', {0: [('7', 'Player')]}, DEADLINE)
            with self.assertRaises(RuntimeError):
                db.delete_recruitment(START, 'glenn', DEADLINE,
                                      expected_record=old, expected_reservation=None)
            self.assertEqual(db.final_roster('100')[0], [('7', 'Player')])
            # Simulate a conflicting reservation from an earlier interrupted operation.
            with db.db:
                db.db.execute("INSERT INTO creation_reservations (raid_type,week_start,parent_channel_id,thread_id,message_id,created_at) VALUES ('glenn', ?, '500', '999', '998', ?)",
                              (START.isoformat(), DEADLINE.isoformat()))
            with self.assertRaises(RuntimeError):
                db.delete_recruitment(START, 'glenn', DEADLINE,
                                      expected_record=db.by_thread('100'),
                                      expected_reservation=db.creation_reservation(START, 'glenn'))
            self.assertIsNotNone(db.by_thread('100'))
            self.assertFalse(db.is_cancelled(START, 'glenn'))
            db.db.close()

    def test_closed_recruitment_still_blocks_new_thread_same_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / 'x.sqlite3')
            db.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            db.finalize('100', {0: [('7', 'Player')]}, DEADLINE)
            self.assertFalse(db.reserve_creation(START, 'glenn', '500', DEADLINE))
            self.assertEqual(db.by_week(START, 'glenn')['status'], 'closed')
            db.db.close()

    def test_old_database_migrates_preserving_recruitments_and_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.sqlite3'
            raw = sqlite3.connect(path)
            raw.executescript('''
                CREATE TABLE recruitments (
                  thread_id TEXT PRIMARY KEY,
                  week_start TEXT NOT NULL UNIQUE,
                  week_end TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  deadline_at TEXT NOT NULL,
                  emoji_ids_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'open',
                  closed_at TEXT,
                  reactions_cleared INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE final_roster (
                  thread_id TEXT NOT NULL,
                  day_index INTEGER NOT NULL,
                  user_id TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  PRIMARY KEY(thread_id,day_index,user_id),
                  FOREIGN KEY(thread_id) REFERENCES recruitments(thread_id)
                );
            ''')
            raw.execute('''
                INSERT INTO recruitments (thread_id, week_start, week_end,
                  message_id, deadline_at, emoji_ids_json, status, closed_at, reactions_cleared)
                VALUES ('900', '2026-11-26', '2026-12-02', '901',
                  '2026-11-25T23:00:00+09:00', '{}', 'closed',
                  '2026-11-25T23:30:00+09:00', 1)
            ''')
            raw.execute("INSERT INTO final_roster VALUES ('900',0,'111','Existing Member')")
            raw.commit()
            raw.close()

            upgraded = Storage(path)
            self.assertEqual(upgraded.by_thread('900')['raid_type'], 'legacy')
            self.assertEqual(upgraded.final_roster('900')[0], [('111', 'Existing Member')])
            upgraded.insert('100', START, '101', DEADLINE, EMOJIS, 'glenn')
            upgraded.insert('200', START, '201', DEADLINE, EMOJIS, '4f')
            self.assertEqual(upgraded.by_week(START, 'glenn')['thread_id'], '100')
            self.assertEqual(upgraded.by_week(START, '4f')['thread_id'], '200')
            self.assertEqual(upgraded.by_thread('900')['status'], 'closed')
            self.assertFalse(upgraded.db.execute('PRAGMA foreign_key_check').fetchall())
            upgraded.db.close()


if __name__ == '__main__':
    unittest.main()
