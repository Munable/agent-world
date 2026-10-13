"""Bounded host timings; no world facts, task arguments or durable trace writes."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import logging
import threading
import time

logger = logging.getLogger(__name__)


class WorkDiagnostics:
    def __init__(self, capacity=128):
        self._recent = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._count = 0
        self._phases = {}
        self._kinds = {}

    @contextmanager
    def transaction(self, kind):
        previous = getattr(self._local, "sample", None)
        sample = {"kind": kind, "started_at": time.time(), "phases": {}}
        started = time.monotonic()
        self._local.sample = sample
        try:
            yield
        except BaseException as exc:
            sample["error"] = type(exc).__name__
            raise
        finally:
            sample["seconds"] = time.monotonic() - started
            self._local.sample = previous
            with self._lock:
                self._count += 1
                self._recent.append(sample)
                kind_total = self._kinds.setdefault(kind, {"count": 0, "seconds": 0.0, "max_seconds": 0.0,
                    "phases": {}, "physical_commit_attempts": 0, "physical_commits": 0,
                    "logical_results_committed": 0})
                kind_total["count"] += 1
                kind_total["seconds"] += sample["seconds"]
                kind_total["max_seconds"] = max(kind_total["max_seconds"], sample["seconds"])
                kind_total["physical_commit_attempts"] += int(sample.get("physical_commit_attempted", False))
                kind_total["physical_commits"] += int(sample.get("physical_commit_completed", False))
                if sample.get("physical_commit_completed"):
                    kind_total["logical_results_committed"] += sample.get("logical_results", 0)
                for name, elapsed in sample["phases"].items():
                    for totals in (self._phases, kind_total["phases"]):
                        total = totals.setdefault(name, {"count": 0, "seconds": 0.0, "max_seconds": 0.0})
                        total["count"] += 1
                        total["seconds"] += elapsed
                        total["max_seconds"] = max(total["max_seconds"], elapsed)
            if sample["seconds"] >= 1 and kind in {"action", "checkpoint", "checkpoint-batch", "timer", "maintenance", "wal-checkpoint", "wal-status", "identity"}:
                phases = sample["phases"]
                logger.warning("world work slow started_at=%.6f started_utc=%s kind=%s function=%s "
                    "seconds=%.3f writer_wait=%.3f begin=%.3f handler=%.3f handler_cpu=%.3f "
                    "managed=%.3f commit=%.3f wal_backfill=%.3f wal_status=%.3f wal_pending=%s",
                    sample["started_at"], datetime.fromtimestamp(sample["started_at"], timezone.utc).isoformat(),
                    kind, sample.get("handler", "-"), sample["seconds"],
                    phases.get("writer_wait", 0), phases.get("begin", 0),
                    phases.get("handler", 0), phases.get("handler_cpu", 0), phases.get("managed", 0),
                    phases.get("sqlite_commit", 0), phases.get("wal_backfill", 0),
                    phases.get("wal_status", 0), sample.get("pending_frames", "-"))

    @contextmanager
    def phase(self, name, *, cpu=False):
        sample = getattr(self._local, "sample", None)
        if sample is None:
            yield
            return
        started = time.monotonic()
        cpu_started = time.thread_time() if cpu else None
        try:
            yield
        finally:
            phases = sample["phases"]
            phases[name] = phases.get(name, 0.0) + time.monotonic() - started
            if cpu:
                phases[name + "_cpu"] = phases.get(name + "_cpu", 0.0) + time.thread_time() - cpu_started

    def annotate(self, **values):
        sample = getattr(self._local, "sample", None)
        if sample is not None:
            # Call sites pass only trusted task labels and numeric watermarks.
            sample.update(values)

    def snapshot(self, limit=32):
        if type(limit) is not int or not 0 <= limit <= self._recent.maxlen:
            raise ValueError("diagnostic sample limit must be between 0 and 128")
        with self._lock:
            return {"capacity": self._recent.maxlen, "transactions": self._count,
                    "phases": deepcopy(self._phases),
                    "kinds": deepcopy(self._kinds),
                    "recent": deepcopy(list(self._recent)[-limit:]) if limit else []}
