"""Persistent storage for independent Glenn / 4F recruitments.

One SQLite DB; a thread ID identifies a single recruitment. The pair
(raid_type, week_start) prevents duplicates without blocking the other raid.
Existing single-raid records are migrated to type `legacy` and preserved.
"""
import json
import sqlite3
from datetime import timedelta
from pathlib import Path


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
              created_at TEXT NOT NULL,
              PRIMARY KEY (raid_type, week_start)
            );
            -- A deliberate deletion suppresses only this week's automatic creation.
            CREATE TABLE IF NOT EXISTS cancelled_weeks (
              raid_type TEXT NOT NULL,
              week_start TEXT NOT NULL,
              cancelled_at TEXT NOT NULL,
              PRIMARY KEY (raid_type, week_start)
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
        self.db.commit()

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
        return self.db.execute(
            'SELECT 1 FROM cancelled_weeks WHERE raid_type=? AND week_start=?',
            (raid_type, start.isoformat())
        ).fetchone() is not None

    def delete_recruitment(self, start, raid_type, now, *, expected_record, expected_reservation):
        """Remove just one confirmed recruitment and its roster/reservation atomically.

        Caller must have deleted the corresponding Discord thread (or confirmed
        it is missing). An explicit cancellation prevents the timer from
        immediately recreating it. New manual creation can clear that marker.
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

    def insert(self, thread_id, start, message_id, deadline_at, emoji_ids, raid_type):
        if raid_type not in ("glenn", "4f"):
            raise ValueError("New recruitments must be for 'glenn' or '4f'")
        with self.db:
            self.db.execute("""
                INSERT INTO recruitments
                (thread_id,raid_type,week_start,week_end,message_id,deadline_at,emoji_ids_json)
                VALUES (?,?,?,?,?,?,?)
            """, (str(thread_id), raid_type, start.isoformat(),
                  (start + timedelta(days=6)).isoformat(), str(message_id),
                  (deadline_at.isoformat() if deadline_at is not None else ""), json.dumps(emoji_ids)))
            # Only a successful creation releases the cancellation marker.
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

        The Discord layer must clear any stale reactions and restore the bot's
        seven weekday reactions before invoking this DB transition.
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

    def finalize(self, thread_id, roster, closed_at):
        """Atomic snapshot and close, completed before deleting reactions."""
        with self.db:
            rec = self.by_thread(thread_id)
            if rec is None:
                raise ValueError(f"Unknown recruitment thread {thread_id}")
            if rec['status'] == 'closed':
                return
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
