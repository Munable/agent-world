from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventSpec:
    recipient_role_id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FunctionOutcome:
    result: dict[str, Any]
    events: tuple[EventSpec, ...] = ()
