"""FIFO admission between atomic writes, with the existing reentrant lock protocol."""
from __future__ import annotations

from collections import deque
import math
import threading
import time


class WriterGate:
    """A waiter cannot be overtaken by the writer's next transaction.

    Reentry belongs to the current transaction and does not queue behind
    itself. The gate never interrupts a handler, commit or rollback.
    """
    _BACKGROUND = frozenset({"checkpoint", "timer", "maintenance"})

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._queue = deque()
        self._owner = None
        self._depth = 0
        self._kind = None

    def acquire(self, blocking=True, timeout=-1, *, kind="write"):
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        if timeout != -1:
            if math.isnan(timeout) or timeout < 0:
                raise ValueError("timeout value must be non-negative")
            if timeout > threading.TIMEOUT_MAX:
                raise OverflowError("timeout value is too large")
        deadline = None if timeout == -1 else time.monotonic() + timeout
        owner = threading.get_ident()
        with self._condition:
            if self._owner == owner:
                self._depth += 1
                return True
            if not blocking:
                if self._owner is not None or self._queue:
                    return False
                self._owner, self._depth, self._kind = owner, 1, kind
                return True
            ticket = (object(), kind)
            self._queue.append(ticket)
            try:
                while self._owner is not None or self._queue[0] is not ticket:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self._queue.remove(ticket)
                        self._condition.notify_all()
                        return False
                    self._condition.wait(remaining)
                self._queue.popleft()
                self._owner, self._depth, self._kind = owner, 1, kind
                return True
            except BaseException:
                # Cancellation of a waiter must not leave an abandoned head.
                if ticket in self._queue:
                    self._queue.remove(ticket)
                self._condition.notify_all()
                raise

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError("cannot release un-acquired lock")
            self._depth -= 1
            if not self._depth:
                self._owner = self._kind = None
                self._condition.notify_all()

    def _is_owned(self):
        with self._condition:
            return self._owner == threading.get_ident()

    def snapshot(self):
        """Only trusted categories and counts; no thread IDs or request data."""
        with self._condition:
            foreground = sum(kind not in self._BACKGROUND for _, kind in self._queue)
            return {"active": self._owner is not None, "kind": self._kind,
                    "waiting": len(self._queue), "foreground_waiting": foreground,
                    "background_waiting": len(self._queue) - foreground}

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
