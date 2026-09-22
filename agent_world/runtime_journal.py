from __future__ import annotations

import json
import time

from .runtime_contracts import identifier, integer, json_text, MAX_RESULT_BYTES
from .runtime_errors import CursorExpired, CursorInvalid, InvalidArguments, PermissionDenied

JOURNAL_TRIGGERS = frozenset({"aw_state_insert", "aw_state_update", "aw_state_delete"})
MAX_COMMIT_CHANGES = 512
MAX_COMMIT_HISTORY_BYTES = 2 * 1024 * 1024
MAX_HISTORY_PAGE_BYTES = 1024 * 1024


class RuntimeJournal:
    """Private durable state history. Never a public subscription or an authorization bypass."""

    def _init_journal_tx(self, c):
        old_version = c.execute("PRAGMA user_version").fetchone()[0]
        c.execute("""CREATE TABLE IF NOT EXISTS world_commits(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, universe TEXT NOT NULL,
            actor_role_id TEXT NOT NULL, function_id TEXT NOT NULL, operation_id TEXT,
            source TEXT NOT NULL, world_version INTEGER, created_at REAL NOT NULL,
            change_count INTEGER NOT NULL, event_seqs_json TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS state_changes(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, universe TEXT NOT NULL,
            scope TEXT NOT NULL, state_key TEXT NOT NULL, kind TEXT NOT NULL,
            before_json TEXT, before_version INTEGER NOT NULL, before_deleted INTEGER NOT NULL,
            value_json TEXT NOT NULL, version INTEGER NOT NULL, deleted INTEGER NOT NULL,
            created_at REAL NOT NULL, commit_seq INTEGER REFERENCES world_commits(seq))""")
        c.execute("""CREATE TABLE IF NOT EXISTS state_history_floors(
            universe TEXT PRIMARY KEY, floor INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_state_changes_world ON state_changes(universe,seq)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_state_changes_key ON state_changes(universe,scope,state_key,seq)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_world_commits ON world_commits(universe,seq)")
        if old_version < 2:
            # Existing current values are a baseline, not a reconstruction of lost history.
            c.execute("""INSERT INTO state_changes(universe,scope,state_key,kind,before_json,
                before_version,before_deleted,value_json,version,deleted,created_at)
                SELECT universe,scope,state_key,'baseline',NULL,0,1,value_json,version,deleted,updated_at
                FROM world_state ORDER BY universe,scope,state_key""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_state_identity BEFORE UPDATE ON world_state
            WHEN NEW.universe != OLD.universe OR NEW.scope != OLD.scope OR NEW.state_key != OLD.state_key
            BEGIN SELECT RAISE(ABORT, 'state identity is immutable; delete and create instead'); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_state_insert AFTER INSERT ON world_state BEGIN
            INSERT INTO state_changes(universe,scope,state_key,kind,before_json,before_version,
                before_deleted,value_json,version,deleted,created_at)
            VALUES(NEW.universe,NEW.scope,NEW.state_key,'create',NULL,0,1,
                NEW.value_json,NEW.version,NEW.deleted,NEW.updated_at); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_state_update AFTER UPDATE ON world_state
            WHEN NEW.value_json IS NOT OLD.value_json OR NEW.version != OLD.version
                OR NEW.deleted != OLD.deleted OR NEW.updated_at != OLD.updated_at
            BEGIN INSERT INTO state_changes(universe,scope,state_key,kind,before_json,before_version,
                before_deleted,value_json,version,deleted,created_at)
            VALUES(NEW.universe,NEW.scope,NEW.state_key,
                CASE WHEN NEW.deleted=1 THEN 'delete' ELSE 'update' END,
                OLD.value_json,OLD.version,OLD.deleted,NEW.value_json,NEW.version,NEW.deleted,NEW.updated_at); END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_state_delete AFTER DELETE ON world_state BEGIN
            INSERT INTO state_changes(universe,scope,state_key,kind,before_json,before_version,
                before_deleted,value_json,version,deleted,created_at)
            VALUES(OLD.universe,OLD.scope,OLD.state_key,'delete',OLD.value_json,OLD.version,
                OLD.deleted,'null',OLD.version+1,1,CAST(strftime('%s','now') AS REAL)); END""")

    @staticmethod
    def _journal_marker_tx(c):
        row = c.execute("SELECT seq FROM sqlite_sequence WHERE name='state_changes'").fetchone()
        return int(row[0]) if row else 0

    def _record_commit_tx(self, c, universe, marker, *, actor_role_id, function_id,
                          operation_id=None, source="action", world_version=None, event_seqs=()):
        if c.execute("SELECT 1 FROM state_changes WHERE seq>? AND universe!=? LIMIT 1", (marker, universe)).fetchone():
            raise PermissionDenied("a world transaction cannot change another universe")
        count, size = c.execute("""SELECT COUNT(*),COALESCE(SUM(
            length(CAST(COALESCE(before_json,'') AS BLOB))+length(CAST(value_json AS BLOB))),0)
            FROM state_changes WHERE seq>?""", (marker,)).fetchone()
        if count > MAX_COMMIT_CHANGES or size > MAX_COMMIT_HISTORY_BYTES:
            raise InvalidArguments("world transaction exceeds its state change budget")
        seq = c.execute("""INSERT INTO world_commits(universe,actor_role_id,function_id,operation_id,
            source,world_version,created_at,change_count,event_seqs_json) VALUES(?,?,?,?,?,?,?,?,?)""",
            (universe, actor_role_id, function_id, operation_id, source, world_version,
             time.time(), count, json_text(list(event_seqs)))).lastrowid
        c.execute("UPDATE state_changes SET commit_seq=? WHERE seq>?", (seq, marker))
        return int(seq)

    def _state_revision_tx(self, c, universe):
        row = c.execute("SELECT floor FROM state_history_floors WHERE universe=?", (universe,)).fetchone()
        floor = int(row[0]) if row else 0
        latest = c.execute("SELECT COALESCE(MAX(seq),0) FROM state_changes WHERE universe=?", (universe,)).fetchone()[0]
        return max(floor, int(latest)), floor

    def read_state_history(self, universe, *, after=0, limit=50):
        """Trusted server/admin inspection only. Values may contain private world state."""
        identifier(universe, "universe")
        integer(after, "after")
        integer(limit, "limit", 1, 200)
        with self._conn(readonly=True) as c:
            c.execute("BEGIN")
            latest, floor = self._state_revision_tx(c, universe)
            if after < floor:
                raise CursorExpired("state history was explicitly pruned; load a current snapshot")
            if after > latest:
                raise CursorInvalid("cursor is ahead of state history")
            rows = c.execute("SELECT * FROM state_changes WHERE universe=? AND seq>? ORDER BY seq LIMIT ?",
                             (universe, after, limit + 1)).fetchall()
            changes, used = [], 0
            for row in rows[:limit]:
                item = dict(row)
                item["before"] = json.loads(item.pop("before_json")) if row["before_json"] is not None else None
                item["value"] = json.loads(item.pop("value_json"))
                encoded = json_text(item, maximum=MAX_COMMIT_HISTORY_BYTES)
                size = len(encoded.encode())
                if changes and used + size > MAX_HISTORY_PAGE_BYTES:
                    break
                changes.append(item)
                used += size
            more = len(changes) < len(rows)
            return {"changes": changes, "next_cursor": changes[-1]["seq"] if more else latest,
                    "has_more": more, "history_floor": floor, "latest_revision": latest}

    def prune_state_history(self, universe, through):
        """Explicit privileged retention operation; notifications and current state are independent."""
        identifier(universe, "universe")
        integer(through, "through")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            latest, floor = self._state_revision_tx(c, universe)
            candidate = c.execute("SELECT MAX(seq) FROM state_changes WHERE universe=? AND seq<=?",
                                  (universe, through)).fetchone()[0]
            if candidate is None:
                return 0
            count = c.execute("DELETE FROM state_changes WHERE universe=? AND seq<=?", (universe, through)).rowcount
            c.execute("INSERT INTO state_history_floors VALUES(?,?) ON CONFLICT(universe) DO UPDATE SET floor=excluded.floor",
                      (universe, max(floor, candidate)))
            return count
