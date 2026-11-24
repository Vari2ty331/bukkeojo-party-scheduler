import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from core.calendar import (KST, is_starter_message, next_thursday, raid_label,
                           schedule_for_week, starter_content, title, week_days)
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

    def test_schedule(self):
        op, cl = schedule_for_week(START, 1, '18:00', 1, '23:00')
        self.assertEqual(op, datetime(2026, 11, 25, 18, tzinfo=KST))
        self.assertEqual(cl, DEADLINE)

    def test_starter_mentions_only_configured_role_when_supplied(self):
        glenn = starter_content(START, DEADLINE, 'glenn', role_id=111222333)
        fourf = starter_content(START, DEADLINE, '4f', role_id=444555666)
        self.assertTrue(glenn.startswith('<@&111222333>\n'))
        self.assertNotIn('<@&444555666>', glenn)
        self.assertTrue(fourf.startswith('<@&444555666>\n'))
        self.assertNotIn('<@&111222333>', fourf)
        self.assertIn('11/26(목)', glenn)
        # Historical posts without role mentions remain unchanged.
        self.assertFalse(starter_content(START, DEADLINE, 'glenn').startswith('<@&'))

    def test_starter_recognition_survives_role_change_and_deadline_edit(self):
        old_starter = starter_content(START, DEADLINE, '4f', role_id=123456789)
        self.assertTrue(is_starter_message(old_starter, START, '4f'))
        self.assertTrue(is_starter_message('🔒 모집 마감\n' + old_starter, START, '4f'))
        self.assertFalse(is_starter_message(old_starter, START, 'glenn'))
        self.assertFalse(is_starter_message(old_starter, START + timedelta(days=7), '4f'))

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
        self.assertIn('글렌 베르나', title(START, 'glenn'))
        self.assertIn('4관', title(START, '4f'))
        self.assertIn('글렌 베르나', starter_content(START, DEADLINE, 'glenn'))
        self.assertIn('4관', starter_content(START, DEADLINE, '4f'))
        self.assertEqual(raid_label('legacy'), '기존 파티')


class StorageTests(unittest.TestCase):
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
