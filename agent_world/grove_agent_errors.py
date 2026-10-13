"""Transport-independent errors for a user-owned Grove client."""
from __future__ import annotations

from copy import deepcopy
import json
import math


def bounded_details(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    def check(node, depth=0):
        if depth > 8:
            return False
        if node is None or type(node) in (str, bool, int):
            return True
        if type(node) is float:
            return math.isfinite(node)
        if isinstance(node, list):
            return all(check(item, depth + 1) for item in node)
        if isinstance(node, dict):
            return all(isinstance(key, str) and check(item, depth + 1) for key, item in node.items())
        return False
    if not check(value):
        return None
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 8192:
        return None
    return deepcopy(value)


class AgentClientError(Exception):
    def __init__(self, code, message, status=400, *, world_details=None,
                 retryable=None, recovery=None, retry_after_seconds=None):
        self.code, self.message, self.status = str(code), str(message), status
        self.world_details = bounded_details(world_details)
        self.retryable = retryable if type(retryable) is bool else None
        self.recovery = recovery if isinstance(recovery, str) else None
        self.retry_after_seconds = (retry_after_seconds if type(retry_after_seconds) in (int, float)
            and math.isfinite(retry_after_seconds) and retry_after_seconds >= 0 else None)
        super().__init__(self.message)

    def public_payload(self):
        result = {"error": self.code, "message": self.message,
                  "retryable": self.retryable if self.retryable is not None else self.status >= 500}
        for name in ("recovery", "retry_after_seconds"):
            if getattr(self, name) is not None:
                result[name] = getattr(self, name)
        if self.world_details is not None:
            result["details"] = deepcopy(self.world_details)
        return result
