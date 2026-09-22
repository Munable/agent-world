from __future__ import annotations
import json
import time
from .runtime_contracts import identifier, integer, duration
from .runtime_errors import CursorExpired, CursorInvalid


class RuntimeEvents:
    def read_changes_page(self, universe, role_id, after_seq=0, limit=50, *, identity_token=None):
        integer(after_seq, "after", 0)
        integer(limit, "limit", 1, 200)
        with self._conn(readonly=True) as c:
            c.execute("BEGIN")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            floor_row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
            floor = int(floor_row["floor"]) if floor_row else 0
            latest = max(
                floor,
                int(
                    c.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=?", (universe,)
                    ).fetchone()[0]
                ),
            )
            if after_seq < floor:
                error = CursorExpired(
                    f"cursor is older than retained floor {floor}; bootstrap a current snapshot"
                )
                error.event_floor = floor
                raise error
            if after_seq > latest:
                raise CursorInvalid("cursor is ahead of the committed event log")
            rows = c.execute(
                "SELECT * FROM events WHERE universe=? AND recipient_role_id=? AND seq>? ORDER BY seq LIMIT ?",
                (universe, role_id, after_seq, limit + 1),
            ).fetchall()
            more = len(rows) > limit
            events = [
                {
                    "seq": int(r["seq"]),
                    "universe": r["universe"],
                    "recipient_role_id": r["recipient_role_id"],
                    "actor_role_id": r["actor_role_id"],
                    "kind": r["kind"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": float(r["created_at"]),
                }
                for r in rows[:limit]
            ]
            cursor = events[-1]["seq"] if more else latest
            return {
                "events": events,
                "next_cursor": cursor,
                "has_more": more,
                "event_floor": floor,
                "latest_event_seq": latest,
            }

    def read_changes(self, universe, role_id, after_seq=0, limit=50, *, identity_token=None):
        return self.read_changes_page(universe, role_id, after_seq, limit, identity_token=identity_token)[
            "events"
        ]

    def wait_changes(
        self, universe, role_id, after_seq=0, timeout=5.0, limit=50, cancel_event=None, *, identity_token=None
    ):
        timeout = duration(timeout, "timeout", 30, zero=True)
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return []
                rows = self.read_changes(universe, role_id, after_seq, limit, identity_token=identity_token)
                if rows:
                    return rows
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                # Durable data is authoritative across processes; notifications are an optimization.
                self._changed.wait(min(0.1, remaining))

    def wake_waiters(self):
        with self._changed:
            self._changed.notify_all()

    def event_floor(self, universe):
        identifier(universe, "universe")
        with self._conn(readonly=True) as c:
            row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
        return int(row["floor"]) if row else 0

    def cleanup_events(self, universe, through_seq):
        identifier(universe, "universe")
        integer(through_seq, "through_seq")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            candidate = c.execute(
                "SELECT MAX(seq) FROM events WHERE universe=? AND seq<=?", (universe, through_seq)
            ).fetchone()[0]
            count = c.execute(
                "DELETE FROM events WHERE universe=? AND seq<=?", (universe, through_seq)
            ).rowcount
            row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
            floor = int(row["floor"]) if row else 0
            if candidate is not None:
                floor = max(floor, int(candidate))
            c.execute(
                "INSERT INTO event_floors(universe,floor) VALUES(?,?) "
                "ON CONFLICT(universe) DO UPDATE SET floor=excluded.floor",
                (universe, floor),
            )
        self.wake_waiters()
        return count
