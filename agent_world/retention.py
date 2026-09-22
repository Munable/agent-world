"""Explicit per-world retention. Never delete authoritative state or idempotency receipts."""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .runtime_contracts import identifier, integer, duration
from .runtime_errors import WorldDefinitionError


@dataclass(frozen=True)
class RetentionPolicy:
    event_seconds: float
    event_rows: int
    history_seconds: float
    history_rows: int

    def contract(self):
        duration(self.event_seconds, "event retention", 315360000, zero=True)
        duration(self.history_seconds, "history retention", 315360000, zero=True)
        integer(self.event_rows, "event rows", 0, 10000000)
        integer(self.history_rows, "history rows", 0, 10000000)
        return asdict(self)


class RuntimeRetention:
    @staticmethod
    def _retention_prefix_tx(c, table, universe, now, seconds, keep_rows, limit):
        # Only contiguous prefixes are removed. Wall-clock rollback cannot create silent gaps.
        total = c.execute("SELECT COUNT(*) FROM " + table + " WHERE universe=?", (universe,)).fetchone()[0]
        excess = max(0, total - keep_rows)
        rows = c.execute("SELECT seq,created_at FROM " + table +
                         " WHERE universe=? ORDER BY seq LIMIT ?", (universe, limit)).fetchall()
        candidate = None
        for index, row in enumerate(rows):
            if index < excess or row["created_at"] <= now - seconds:
                candidate = row["seq"]
            else:
                break
        return candidate

    def apply_retention(self, universe, *, limit=500):
        """Trusted bounded maintenance. Omitted policy means no automatic destructive cleanup."""
        import time
        identifier(universe, "universe")
        integer(limit, "limit", 1, 5000)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._check_world_tx(c, universe)
            definition = self._worlds.get(universe)
            if definition is None:
                raise WorldDefinitionError("retention needs the matching installed world")
            policy = definition.retention
            if policy is None:
                return {"configured": False, "events_removed": 0, "history_removed": 0}
            policy.contract()
            now = time.time()
            removed = {}
            for table, floor_table, seconds, count, label in (
                ("events", "event_floors", policy.event_seconds, policy.event_rows, "events_removed"),
                ("state_changes", "state_history_floors", policy.history_seconds, policy.history_rows, "history_removed"),
            ):
                candidate = self._retention_prefix_tx(c, table, universe, now, seconds, count, limit)
                removed[label] = 0
                if candidate is None:
                    continue
                removed[label] = c.execute("DELETE FROM " + table + " WHERE universe=? AND seq<=?",
                                           (universe, candidate)).rowcount
                c.execute("INSERT INTO " + floor_table + "(universe,floor) VALUES(?,?) "
                          "ON CONFLICT(universe) DO UPDATE SET floor=MAX(floor,excluded.floor)",
                          (universe, candidate))
        if any(removed.values()):
            self.wake_waiters()
        return {"configured": True, **removed}
