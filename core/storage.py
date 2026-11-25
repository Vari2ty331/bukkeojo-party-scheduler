"""Persistent storage for independent Glenn / 4F recruitments.

One SQLite DB; a thread ID identifies a single recruitment. The pair
(raid_type, week_start) prevents duplicates without blocking the other raid.
Existing single-raid records are migrated to type `legacy` and preserved.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from core.calendar import (DEFAULT_GLENN_RUNS, MAX_GLENN_MEMO_LENGTH, FOURF_RUN_EMOJIS,
                           RAID_TYPES, opening_for_week)


SCHEMA = """
    CREATE TABLE IF NOT EXISTS recruitments (
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
      reopened_with_snapshot INTEGER NOT NULL DEFAULT 0,
      preference_message_id TEXT,
      UNIQUE(raid_type, week_start)
    );
"""


def merge_rosters(saved_roster, current_roster):
    """Combine previously saved signups with new reactions, by Discord user ID.

    Old users do not have to react again after their reactions were cleared.
    A new reaction by the same user on a saved day does not count twice.
    """
    result = {}
    for day in range(7):
        people = {str(uid): name for uid, name in saved_roster.get(day, [])}
        people.update((str(uid), name) for uid, name in current_roster.get(day, []))
        result[day] = sorted(people.items(), key=lambda person: (person[1].casefold(), person[0]))
    return result


def merge_run_preferences(saved, current):
    """One desired run group per user; highest reaction within each submission.

    A new explicit selection overrides the saved selection after reopening.
    No new selection keeps the saved preference. 5 means five or more runs.
    """
    def by_user(roster):
        result = {}
        for count in sorted(FOURF_RUN_EMOJIS):
            for uid, name in roster.get(count, []):
                result[str(uid)] = (count, name)
        return result

    selections = by_user(saved)
    selections.update(by_user(current))
    result = {count: [] for count in FOURF_RUN_EMOJIS}
    for uid, (count, name) in selections.items():
        result[count].append((uid, name))
    for people in result.values():
        people.sort(key=lambda entry: (entry[1].casefold(), entry[0]))
    return result


class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")

        # Before creating tables, inspect the old starter's schema. Its
        # UNIQUE(week_start) prevents two raids in the same week.
        table_exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recruitments'"
        ).fetchone()
        if table_exists:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(recruitments)")}
            if "raid_type" not in columns:
                self._migrate_old_recruitments()

        self.db.executescript(SCHEMA + """
            -- Reserve the week/dungeon key BEFORE making the Discord request.
            CREATE TABLE IF NOT EXISTS creation_reservations (
              raid_type TEXT NOT NULL,
              week_start TEXT NOT NULL,
              parent_channel_id TEXT NOT NULL,
              thread_id TEXT,
              message_id TEXT,
              preference_message_id TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (raid_type, week_start)
            );
            -- Deletion timestamps: only deletions at/after opening suppress auto-creation.
            CREATE TABLE IF NOT EXISTS cancelled_weeks (
              raid_type TEXT NOT NULL,
              week_start TEXT NOT NULL,
              cancelled_at TEXT NOT NULL,
              PRIMARY KEY (raid_type, week_start)
            );
            CREATE TABLE IF NOT EXISTS glenn_settings (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              run_count INTEGER NOT NULL CHECK(run_count>0),
              memo TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS final_run_preferences (
              thread_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              desired_runs INTEGER NOT NULL CHECK(desired_runs IN (3,4,5)),
              display_name TEXT NOT NULL,
              PRIMARY KEY(thread_id, user_id),
              FOREIGN KEY(thread_id) REFERENCES recruitments(thread_id)
            );
            CREATE TABLE IF NOT EXISTS final_roster (
              thread_id TEXT NOT NULL,
              day_index INTEGER NOT NULL,
              user_id TEXT NOT NULL,
              display_name TEXT NOT NULL,
              PRIMARY KEY(thread_id, day_index, user_id),
              FOREIGN KEY(thread_id) REFERENCES recruitments(thread_id)
            );
        """)
        # Add the reopen flag for upgrades from every earlier phase-1 database.
        # The original closed roster is kept as a baseline during reopened recruitment.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(recruitments)')}
        if 'reopened_with_snapshot' not in columns:
            with self.db:
                self.db.execute("""ALTER TABLE recruitments ADD COLUMN reopened_with_snapshot
                                   INTEGER NOT NULL DEFAULT 0""")
        # New columns are nullable so historical recruitments remain valid.
        for table in ('recruitments', 'creation_reservations'):
            columns = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
            if 'preference_message_id' not in columns:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN preference_message_id TEXT')
        self.db.execute('INSERT OR IGNORE INTO glenn_settings (singleton,run_count,memo) VALUES (1,?,?)',
                        (DEFAULT_GLENN_RUNS, ''))
        self.db.commit()

    def glenn_settings(self):
        row = self.db.execute('SELECT run_count,memo FROM glenn_settings WHERE singleton=1').fetchone()
        return row['run_count'], row['memo']

    def update_glenn_settings(self, *, run_count=None, memo=None, clear_memo=False):
        if run_count is not None and (type(run_count) is not int or not 1 <= run_count <= 99):
            raise ValueError('횟수는 1~99 사이의 정수로 입력해 주세요.')
        if clear_memo and memo is not None:
            raise ValueError('메모 입력과 메모초기화를 동시에 사용할 수 없습니다.')
        if memo is not None and len(memo) > MAX_GLENN_MEMO_LENGTH:
            raise ValueError(f'메모는 {MAX_GLENN_MEMO_LENGTH}자 이내로 입력해 주세요.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old_count, old_memo = self.glenn_settings()
            count = old_count if run_count is None else run_count
            new_memo = '' if clear_memo else (old_memo if memo is None else memo.strip())
            self.db.execute('UPDATE glenn_settings SET run_count=?, memo=? WHERE singleton=1',
                            (count, new_memo))
        return count, new_memo

    def _migrate_old_recruitments(self):
        """Rebuild old table, preserving thread IDs, metadata and roster rows.

        Old records are assigned `legacy`, allowing *new* Glenn and 4F records
        for the same week without reassigning an existing historical thread.
        """
        with self.db:
            self.db.execute("""
                CREATE TABLE recruitments_v2 (
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
                )
            """)
            self.db.execute("""
                INSERT INTO recruitments_v2
                (thread_id, raid_type, week_start, week_end, message_id, deadline_at,
                 emoji_ids_json, status, closed_at, reactions_cleared)
                SELECT thread_id, 'legacy', week_start, week_end, message_id,
                       deadline_at, emoji_ids_json, status, closed_at,
                       reactions_cleared FROM recruitments
            """)
            self.db.execute("DROP TABLE recruitments")
            self.db.execute("ALTER TABLE recruitments_v2 RENAME TO recruitments")

    def thread_is_recorded(self, thread_id):
        """Do not adopt any thread already assigned to a recruitment or reservation."""
        return self.db.execute(
            'SELECT 1 FROM recruitments WHERE thread_id=? '
            'UNION ALL SELECT 1 FROM creation_reservations WHERE thread_id=? LIMIT 1',
            (str(thread_id), str(thread_id)),
        ).fetchone() is not None

    def creation_reservation(self, start, raid_type):
        return self.db.execute(
            'SELECT * FROM creation_reservations WHERE raid_type=? AND week_start=?',
            (raid_type, start.isoformat())
        ).fetchone()

    def reserve_creation(self, start, raid_type, parent_channel_id, now, *, allow_cancelled=False):
        '''Atomically claim the dungeon/week before making a Discord request.'''
        if raid_type not in ('glenn', '4f'):
            raise ValueError('Unknown dungeon')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.by_week(start, raid_type) or (not allow_cancelled and self.is_cancelled(start, raid_type)):
                return False
            cur = self.db.execute('''
                INSERT OR IGNORE INTO creation_reservations
                  (raid_type, week_start, parent_channel_id, created_at)
                VALUES (?,?,?,?)
            ''', (raid_type, start.isoformat(), str(parent_channel_id), now.isoformat()))
            # A different process could have finished between the initial
            # SELECT and this insert; don't allow a new creation in that case.
            if cur.rowcount == 1 and self.by_week(start, raid_type):
                self.db.execute('''
                    DELETE FROM creation_reservations
                    WHERE raid_type=? AND week_start=?
                ''', (raid_type, start.isoformat()))
                return False
            return cur.rowcount == 1

    def reserve_deleted_thread_replacement(self, start, raid_type, old_thread_id,
                                           parent_channel_id, now):
        """Atomically replace a never-finalized, confirmed-missing thread with a creation claim.

        The caller must first verify Discord returned Unknown Channel for the
        stored thread. Closed or previously reopened rosters are never discarded.
        """
        old_thread_id = str(old_thread_id)
        key = (raid_type, start.isoformat())
        with self.db:
            # Acquire the write lock before reading state in case another
            # process is finalizing or attempting a replacement.
            self.db.execute('BEGIN IMMEDIATE')
            rec = self.by_week(start, raid_type)
            if (rec is None or rec['thread_id'] != old_thread_id
                    or rec['status'] not in ('open', 'review_needed')
                    or rec['closed_at'] is not None or rec['reopened_with_snapshot']
                    or self.db.execute('SELECT 1 FROM final_roster WHERE thread_id=? LIMIT 1',
                                       (old_thread_id,)).fetchone()):
                return False
            job = self.creation_reservation(start, raid_type)
            if job is not None:
                # A crash after inserting the old record may have left its own
                # reservation. Never remove an unrelated or uncertain claim.
                if job['thread_id'] != old_thread_id or job['message_id'] != rec['message_id']:
                    return False
                self.db.execute('DELETE FROM creation_reservations WHERE raid_type=? AND week_start=?', key)
            self.db.execute('DELETE FROM recruitments WHERE thread_id=?', (old_thread_id,))
            self.db.execute("""
                INSERT INTO creation_reservations
                  (raid_type, week_start, parent_channel_id, created_at)
                VALUES (?,?,?,?)
            """, (*key, str(parent_channel_id), now.isoformat()))
            return True

    def is_cancelled(self, start, raid_type):
        """Whether a deletion blocks the scheduled automatic creation.

        A test recruitment deleted before its dungeon's opening must not
        cancel that opening. Existing deletion timestamps from older versions
        are evaluated by the same rule; no database migration is required.
        A deletion at or after opening still prevents repeated automatic posts.
        """
        row = self.db.execute(
            'SELECT cancelled_at FROM cancelled_weeks WHERE raid_type=? AND week_start=?',
            (raid_type, start.isoformat())
        ).fetchone()
        if row is None:
            return False
        if raid_type not in RAID_TYPES:
            return True  # Legacy recruitments have no automatic opening.
        # Older versions also saved timezone-aware ISO timestamps. If a
        # manually edited/corrupt record has no valid timestamp, fail closed.
        try:
            deleted_at = datetime.fromisoformat(row['cancelled_at'])
            if deleted_at.tzinfo is None or deleted_at.utcoffset() is None:
                return True
        except (ValueError, TypeError):
            return True
        return deleted_at >= opening_for_week(start, raid_type)

    def delete_recruitment(self, start, raid_type, now, *, expected_record, expected_reservation):
        """Remove just one confirmed recruitment and its roster/reservation atomically.

        Caller must have deleted the corresponding Discord thread (or confirmed
        it is missing). Record the deletion time: before opening it does not
        block the scheduled post, at/after opening it prevents repeated posts.
        A successful new creation clears that marker.
        """
        if raid_type not in ('glenn', '4f', 'legacy'):
            raise ValueError('Unknown dungeon')
        key = (raid_type, start.isoformat())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rec = self.by_week(start, raid_type)
            job = self.creation_reservation(start, raid_type)
            snapshot = lambda row: dict(row) if row is not None else None
            if (snapshot(rec) != snapshot(expected_record)
                    or snapshot(job) != snapshot(expected_reservation)):
                raise RuntimeError('Recruitment changed during deletion; retry after checking it')
            if rec is None and job is None:
                return False
            if rec is not None and job is not None and (
                    job['thread_id'] != rec['thread_id']
                    or job['message_id'] != rec['message_id']):
                raise RuntimeError('Conflicting creation reservation; inspect before deleting')
            if rec is not None:
                self.db.execute('DELETE FROM final_run_preferences WHERE thread_id=?', (rec['thread_id'],))
                self.db.execute('DELETE FROM final_roster WHERE thread_id=?', (rec['thread_id'],))
                self.db.execute('DELETE FROM recruitments WHERE thread_id=?', (rec['thread_id'],))
            self.db.execute('DELETE FROM creation_reservations WHERE raid_type=? AND week_start=?', key)
            self.db.execute("""
                INSERT INTO cancelled_weeks (raid_type, week_start, cancelled_at)
                VALUES (?,?,?) ON CONFLICT(raid_type, week_start)
                DO UPDATE SET cancelled_at=excluded.cancelled_at
            """, (*key, now.isoformat()))
            return True

    def record_created_thread(self, start, raid_type, thread_id):
        with self.db:
            cur = self.db.execute('''
                UPDATE creation_reservations SET thread_id=?
                WHERE raid_type=? AND week_start=?
                  AND (thread_id IS NULL OR thread_id=?)
            ''', (str(thread_id), raid_type, start.isoformat(), str(thread_id)))
            if cur.rowcount != 1:
                raise RuntimeError('Missing or conflicting creation reservation')

    def record_recruitment_message(self, start, raid_type, message_id):
        with self.db:
            cur = self.db.execute('''
                UPDATE creation_reservations SET message_id=?
                WHERE raid_type=? AND week_start=?
                  AND (message_id IS NULL OR message_id=?)
            ''', (str(message_id), raid_type, start.isoformat(), str(message_id)))
            if cur.rowcount != 1:
                raise RuntimeError('Missing or conflicting creation reservation')

    def record_preference_message(self, start, message_id):
        """Persist the 4F survey message before committing the recruitment."""
        with self.db:
            cur = self.db.execute("""
                UPDATE creation_reservations SET preference_message_id=?
                WHERE raid_type='4f' AND week_start=?
                  AND (preference_message_id IS NULL OR preference_message_id=?)
            """, (str(message_id), start.isoformat(), str(message_id)))
            if cur.rowcount != 1:
                raise RuntimeError('Missing or conflicting preference message reservation')

    def release_creation_reservation(self, start, raid_type):
        '''Release only after the actual recruitment row is committed.'''
        with self.db:
            self.db.execute('''
                DELETE FROM creation_reservations
                WHERE raid_type=? AND week_start=?
            ''', (raid_type, start.isoformat()))

    def by_thread(self, thread_id):
        return self.db.execute(
            "SELECT * FROM recruitments WHERE thread_id=?", (str(thread_id),)
        ).fetchone()

    def by_week(self, start, raid_type):
        return self.db.execute(
            "SELECT * FROM recruitments WHERE raid_type=? AND week_start=?",
            (raid_type, start.isoformat())
        ).fetchone()

    def insert(self, thread_id, start, message_id, deadline_at, emoji_ids, raid_type, *,
               preference_message_id=None):
        if raid_type not in ("glenn", "4f"):
            raise ValueError("New recruitments must be for 'glenn' or '4f'")
        if preference_message_id is not None and raid_type != '4f':
            raise ValueError('Only 4F has a run preference survey')
        with self.db:
            self.db.execute("""
                INSERT INTO recruitments
                (thread_id,raid_type,week_start,week_end,message_id,deadline_at,emoji_ids_json,preference_message_id)
                VALUES (?,?,?,?,?,?,?,?)
            """, (str(thread_id), raid_type, start.isoformat(),
                  (start + timedelta(days=6)).isoformat(), str(message_id),
                  (deadline_at.isoformat() if deadline_at is not None else ""), json.dumps(emoji_ids),
                  str(preference_message_id) if preference_message_id is not None else None))
            # A successful creation releases any prior deletion marker.
            self.db.execute('DELETE FROM cancelled_weeks WHERE raid_type=? AND week_start=?',
                            (raid_type, start.isoformat()))

    def open_recruitments(self):
        return self.db.execute(
            "SELECT * FROM recruitments WHERE status='open'"
        ).fetchall()

    def needs_review(self, thread_id):
        with self.db:
            self.db.execute(
                "UPDATE recruitments SET status='review_needed' "
                "WHERE thread_id=? AND status='open'", (str(thread_id),)
            )

    def reopen(self, thread_id, deadline_at, emoji_ids):
        """Reopen a CLOSED recruitment while retaining its final roster baseline.

        The Discord layer clears stale reactions before this transition and
        restores both sets of options afterward. The saved snapshots remain.
        """
        with self.db:
            rec = self.by_thread(thread_id)
            if rec is None:
                raise ValueError(f'Unknown recruitment thread {thread_id}')
            if rec['status'] != 'closed':
                raise ValueError('Only a closed recruitment can be reopened')
            cur = self.db.execute("""
                UPDATE recruitments SET status='open', closed_at=NULL,
                   reopened_with_snapshot=1, reactions_cleared=0,
                   deadline_at=?, emoji_ids_json=?
                WHERE thread_id=? AND status='closed'
            """, ((deadline_at.isoformat() if deadline_at is not None else ""), json.dumps(emoji_ids), str(thread_id)))
            if cur.rowcount != 1:
                raise RuntimeError('Reopen state transition failed')

    def extend_reopened_deadline(self, thread_id, deadline_at):
        """Extend the existing reopened recruitment without changing its roster."""
        with self.db:
            cur = self.db.execute("""
                UPDATE recruitments SET deadline_at=?
                WHERE thread_id=? AND status='open' AND reopened_with_snapshot=1
            """, ((deadline_at.isoformat() if deadline_at is not None else ""), str(thread_id)))
            if cur.rowcount != 1:
                raise ValueError('The recruitment is not currently reopened')

    def finalize(self, thread_id, roster, closed_at, *, run_preferences=None):
        """Atomic snapshot and close, completed before deleting reactions."""
        with self.db:
            rec = self.by_thread(thread_id)
            if rec is None:
                raise ValueError(f"Unknown recruitment thread {thread_id}")
            if rec['status'] == 'closed':
                return
            if rec['preference_message_id'] is not None and run_preferences is None:
                raise ValueError('The 4F preference survey must be captured before closing')
            if run_preferences is not None and rec['raid_type'] != '4f':
                raise ValueError('Only 4F has a run preference survey')
            self.db.execute("DELETE FROM final_run_preferences WHERE thread_id=?", (str(thread_id),))
            if run_preferences is not None:
                for count, users in merge_run_preferences({}, run_preferences).items():
                    for uid, name in users:
                        self.db.execute("""
                            INSERT INTO final_run_preferences (thread_id,user_id,desired_runs,display_name)
                            VALUES (?,?,?,?)
                        """, (str(thread_id), str(uid), count, name))
            self.db.execute("DELETE FROM final_roster WHERE thread_id=?", (str(thread_id),))
            for day_index, users in roster.items():
                for uid, name in users:
                    self.db.execute("""
                        INSERT INTO final_roster (thread_id,day_index,user_id,display_name)
                        VALUES (?,?,?,?)
                    """, (str(thread_id), int(day_index), str(uid), name))
            self.db.execute(
                "UPDATE recruitments SET status='closed', closed_at=?, reopened_with_snapshot=0, "
                "reactions_cleared=0 WHERE thread_id=?",
                (closed_at.isoformat(), str(thread_id))
            )

    def mark_reactions_cleared(self, thread_id):
        with self.db:
            self.db.execute(
                "UPDATE recruitments SET reactions_cleared=1 WHERE thread_id=?",
                (str(thread_id),)
            )

    def final_roster(self, thread_id):
        rows = self.db.execute("""
            SELECT day_index,user_id,display_name FROM final_roster
            WHERE thread_id=? ORDER BY day_index,display_name
        """, (str(thread_id),)).fetchall()
        roster = {i: [] for i in range(7)}
        for row in rows:
            roster[row['day_index']].append((row['user_id'], row['display_name']))
        return roster

    def final_run_preferences(self, thread_id):
        rows = self.db.execute("""
            SELECT desired_runs,user_id,display_name FROM final_run_preferences
            WHERE thread_id=? ORDER BY desired_runs,display_name
        """, (str(thread_id),)).fetchall()
        result = {count: [] for count in FOURF_RUN_EMOJIS}
        for row in rows:
            result[row['desired_runs']].append((row['user_id'], row['display_name']))
        return result
