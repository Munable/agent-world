"""Optional, retained shared event channels. They do not schedule or impersonate Agents."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import inspect
import re
from .runtime_contracts import identifier, integer, duration, json_text
from .runtime_errors import WorldDefinitionError, CursorInvalid, FunctionNotFound

class StreamResetRequired(CursorInvalid):
    pass

class StreamNotFound(FunctionNotFound):
    pass

@dataclass(frozen=True)
class StreamSpec:
    name: str
    public: bool = False
    authorize: object = None
    retention_seconds: float = 86400
    max_events: int = 4096

    def contract(self):
        identifier(self.name, 'stream', 64)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', self.name) or type(self.public) is not bool:
            raise WorldDefinitionError('invalid stream name or public flag')
        if self.authorize is not None and (not callable(self.authorize) or inspect.iscoroutinefunction(self.authorize)):
            raise WorldDefinitionError('stream authorization must be synchronous')
        duration(self.retention_seconds, 'stream retention', 31536000)
        integer(self.max_events, 'stream capacity', 1, 100000)
        return {'name': self.name, 'public': self.public,
                'retention_seconds': self.retention_seconds, 'max_events': self.max_events}

@dataclass(frozen=True)
class StreamEvent:
    stream: str
    kind: str
    payload: dict
    key: str = ''


def event_id(universe, actor, function, operation, stream, key):
    identifier(stream, 'stream', 64)
    identifier(key, 'event key')
    if not operation:
        raise WorldDefinitionError('shared publications require a managed operation')
    return 'awe_' + hashlib.sha256(json_text([universe, actor, function, operation, stream, key]).encode()).hexdigest()
