"""Explicit per-world retention. Never delete authoritative state or idempotency receipts."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import time

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
    def _init_checkpoint_retention_tx(c):
        """Current eligibility follows references in their own transactions.

        This index excludes millions of current receipts. A released old
        latest receipt, or an expired stream reference, becomes discoverable
        again even if the worker crashes before its maintenance pass.
        """
        c.execute("CREATE INDEX IF NOT EXISTS idx_checkpoint_reclaimable "
                  "ON world_commits(universe,checkpoint_reclaimable,seq) WHERE source='checkpoint'")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_checkpoint_reference_insert AFTER INSERT ON world_checkpoints
            WHEN NEW.latest_commit_seq IS NOT NULL BEGIN
            UPDATE world_commits SET checkpoint_reclaimable=0
                WHERE seq=NEW.latest_commit_seq AND source='checkpoint'; END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_checkpoint_reference_update AFTER UPDATE OF latest_commit_seq ON world_checkpoints
            WHEN OLD.latest_commit_seq IS NOT NEW.latest_commit_seq BEGIN
            UPDATE world_commits SET checkpoint_reclaimable=1
                WHERE seq=OLD.latest_commit_seq AND source='checkpoint';
            UPDATE world_commits SET checkpoint_reclaimable=0
                WHERE seq=NEW.latest_commit_seq AND source='checkpoint'; END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_checkpoint_reference_delete AFTER DELETE ON world_checkpoints
            WHEN OLD.latest_commit_seq IS NOT NULL BEGIN
            UPDATE world_commits SET checkpoint_reclaimable=1
                WHERE seq=OLD.latest_commit_seq AND source='checkpoint'; END""")
        c.execute("""CREATE TRIGGER IF NOT EXISTS aw_checkpoint_stream_release AFTER DELETE ON stream_events
            WHEN OLD.commit_seq IS NOT NULL BEGIN
            UPDATE world_commits SET checkpoint_reclaimable=1
                WHERE seq=OLD.commit_seq AND source='checkpoint'; END""")

    @staticmethod
    def _retention_prefix_tx(c, table, universe, now, seconds, keep_rows, limit):
        # Only contiguous prefixes are removed. Wall-clock rollback cannot create silent gaps.
        # A seek through at most the configured retained window, never a full
        # COUNT over the lifetime journal. This boundary remains universe-local.
        excess_row = c.execute("SELECT seq FROM " + table +
                               " WHERE universe=? ORDER BY seq DESC LIMIT 1 OFFSET ?",
                               (universe, keep_rows)).fetchone()
        excess_through = int(excess_row[0]) if excess_row else 0
        rows = c.execute("SELECT seq,created_at FROM " + table +
                         " WHERE universe=? ORDER BY seq LIMIT ?", (universe, limit)).fetchall()
        candidate = None
        for row in rows:
            if row["seq"] <= excess_through or row["created_at"] <= now - seconds:
                candidate = row["seq"]
            else:
                break
        return candidate

    def apply_retention(self, universe, *, limit=500):
        """Trusted bounded maintenance. Omitted policy means no automatic destructive cleanup."""
        import time
        identifier(universe, "universe")
        integer(limit, "limit", 1, 5000)
        with self._conn(serialized=True) as c:
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

    def apply_checkpoint_retention(self, universe, *, keep_commits=4096, seconds=86400,
                                   limit=64, change_limit=4096):
        """Retain bounded new checkpoint observations, preserving all old sources.

        Current work receipts and foreign-key consumers keep their commits.
        Reference release atomically reopens the indexed eligibility bit, so
        this seek never walks the entire protected latest-receipt directory.
        The host supplies a work-proportional scan/change budget plus catch-up;
        its hard bounds apply even while recovering an old backlog.
        """
        self._checkpoint_retention_contract(universe, keep_commits, seconds, limit, change_limit)
        with self._conn(serialized=True, purpose="maintenance") as c:
            with self._work_timings.phase("begin"):
                c.execute("BEGIN IMMEDIATE")
            self._check_world_tx(c, universe)
            with self._work_timings.phase("checkpoint_retention"):
                result = self._apply_checkpoint_retention_tx(c, universe, keep_commits=keep_commits,
                    seconds=seconds, limit=limit, change_limit=change_limit)
        if result["checkpoint_changes_removed"]:
            self.wake_waiters()
        return {key: result[key] for key in ("checkpoint_commits_removed", "checkpoint_changes_removed",
            "checkpoint_completeness_floor", "checkpoint_commit_floor", "scanned_through")}

    @staticmethod
    def _checkpoint_retention_contract(universe, keep_commits, seconds, limit, change_limit):
        identifier(universe, "universe")
        integer(keep_commits, "checkpoint retained commits", 0, 100000)
        duration(seconds, "checkpoint retention", 315360000, zero=True)
        integer(limit, "checkpoint retention batch", 1, 256)
        integer(change_limit, "checkpoint retained-change batch", 1, 106496)

    def apply_work_retention(self, universe, *, stream_limit=500, limit=64, change_limit=4096,
                             keep_commits=4096, seconds=86400, time_budget=0.025, stream_after=None):
        """One bounded maintenance commit; individual world tasks stay atomic.

        Streams release foreign-key references before checkpoint collection.
        Deadline checks yield between bounded SQL units, never abandon a
        partial commit or change FULL durability. Scan counters let the host
        retain unspent production credit when a pass leaves eligible backlog.
        """
        self._checkpoint_retention_contract(universe, keep_commits, seconds, limit, change_limit)
        integer(stream_limit, "stream retention batch", 1, 5000)
        duration(time_budget, "maintenance time budget", 1)
        if stream_after is not None:
            identifier(stream_after, "maintenance stream cursor")
        with self._conn(serialized=True, purpose="maintenance") as c:
            with self._work_timings.phase("begin"):
                c.execute("BEGIN IMMEDIATE")
            self._check_world_tx(c, universe)
            # Lock wait is measured separately and cannot spend the entire GC
            # budget: otherwise a busy writer can starve recovery forever.
            deadline = time.monotonic() + time_budget
            # Reserve a share for checkpoint reclaim even when streams are
            # continuously busy; a SQL unit itself cannot be preempted.
            stream_deadline = time.monotonic() + time_budget / 2
            with self._work_timings.phase("stream_retention"):
                streams = self._apply_stream_retention_tx(c, universe, limit=stream_limit,
                    deadline=stream_deadline, shared_limit=True, after_stream=stream_after)
            with self._work_timings.phase("checkpoint_retention"):
                checkpoints = self._apply_checkpoint_retention_tx(c, universe, keep_commits=keep_commits,
                    seconds=seconds, limit=limit, change_limit=change_limit, deadline=deadline)
            self._work_timings.annotate(stream_events_removed=streams["stream_events_removed"],
                checkpoint_commits_removed=checkpoints["checkpoint_commits_removed"],
                checkpoint_changes_removed=checkpoints["checkpoint_changes_removed"])
        if checkpoints["checkpoint_changes_removed"]:
            self.wake_waiters()
        return {**streams, **checkpoints}

    def _apply_checkpoint_retention_tx(self, c, universe, *, keep_commits, seconds, limit,
                                       change_limit, deadline=None):
        removed_commits = removed_changes = 0
        current = c.execute("SELECT floor,commit_floor,scan_after FROM checkpoint_history_floors WHERE universe=?",
                            (universe,)).fetchone()
        floor, commit_floor, scan_after = tuple(current) if current else (0, 0, 0)
        boundary = c.execute("SELECT seq FROM world_commits WHERE universe=? AND source='checkpoint' "
                             "ORDER BY seq DESC LIMIT 1 OFFSET ?", (universe, keep_commits)).fetchone()
        count_through = int(boundary[0]) if boundary else 0
        rows = c.execute("SELECT seq,actor_role_id,operation_id,created_at,change_count FROM world_commits "
                         "WHERE universe=? AND source='checkpoint' AND checkpoint_reclaimable=1 "
                         "AND seq>? ORDER BY seq LIMIT ?", (universe, scan_after, limit)).fetchall()
        now = time.time()
        scanned = scan_after
        inspected = 0
        reached_fresh = stopped = False
        for row in rows:
            # Attempt one bounded unit even if a stream SQL unit was slow;
            # continuous stream backlog must not starve checkpoint recovery.
            if inspected and deadline is not None and time.monotonic() >= deadline:
                stopped = True
                break
            if row['seq'] > count_through and row['created_at'] > now-seconds:
                reached_fresh = True
                break
            references = (
                ("SELECT 1 FROM world_checkpoints WHERE universe=? AND latest_commit_seq=? LIMIT 1",
                 (universe, row['seq'])),
                ("SELECT 1 FROM stream_events WHERE commit_seq=? LIMIT 1", (row['seq'],)),
                ("SELECT 1 FROM timer_transitions WHERE commit_seq=? LIMIT 1", (row['seq'],)),
                ("SELECT 1 FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=? LIMIT 1",
                 (universe, row['actor_role_id'], row['operation_id'])),
            )
            if any(c.execute(sql, params).fetchone() is not None for sql, params in references):
                inspected += 1
                scanned = int(row['seq'])
                c.execute("UPDATE world_commits SET checkpoint_reclaimable=0 WHERE seq=?", (row['seq'],))
                continue
            if removed_changes + row['change_count'] > change_limit:
                stopped = True
                break
            inspected += 1
            scanned = int(row['seq'])
            last = c.execute("SELECT MAX(seq) FROM state_changes WHERE commit_seq=?", (row['seq'],)).fetchone()[0]
            removed_changes += c.execute("DELETE FROM state_changes WHERE commit_seq=?", (row['seq'],)).rowcount
            removed_commits += c.execute("DELETE FROM world_commits WHERE seq=?", (row['seq'],)).rowcount
            floor = max(floor, int(last) if last is not None else 0)
            commit_floor = max(commit_floor, int(row['seq']))
        backlog = stopped or (not reached_fresh and len(rows) == limit)
        if not rows or reached_fresh or (not stopped and len(rows) < limit):
            scanned = 0
        # No repeated journal entry: this is current maintenance position.
        if (floor, commit_floor, scanned) != (tuple(current) if current else (0, 0, 0)):
            c.execute("INSERT INTO checkpoint_history_floors(universe,floor,commit_floor,scan_after) VALUES(?,?,?,?) "
                      "ON CONFLICT(universe) DO UPDATE SET floor=excluded.floor,commit_floor=excluded.commit_floor,"
                      "scan_after=excluded.scan_after", (universe, floor, commit_floor, scanned))
        return {"checkpoint_commits_removed": removed_commits, "checkpoint_changes_removed": removed_changes,
                "checkpoint_completeness_floor": floor, "checkpoint_commit_floor": commit_floor,
                "scanned_through": scanned, "checkpoint_commits_scanned": inspected,
                "checkpoint_backlog": backlog}
