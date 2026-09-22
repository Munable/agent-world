"""Public expression cues, not private model reasoning or renderer frames."""
from __future__ import annotations
from dataclasses import dataclass, field
from .runtime_contracts import validate
from .world_types import EventSpec
from .world_streams import StreamEvent

PRESENTATION_KIND = "world.presentation"
ID = {"type": "string", "minLength": 1, "maxLength": 128}
CUE_SCHEMA = {
    "type": "object", "properties": {
        "version": {"const": 1}, "cue_id": ID, "subject_id": ID,
        "channel": {"enum": ["action", "speech", "intent"]},
        "phase": {"enum": ["start", "finish", "cancel"]}, "name": ID,
        "data": {"type": "object"},
    },
    "required": ["version", "cue_id", "subject_id", "channel", "phase", "name", "data"],
    "additionalProperties": False,
}


def validate_cue(payload):
    validate(CUE_SCHEMA, payload, maximum=16384)


@dataclass(frozen=True)
class PresentationCue:
    cue_id: str
    subject_id: str
    channel: str
    phase: str
    name: str
    data: dict = field(default_factory=dict)

    def publish(self, stream: str, *, key="") -> StreamEvent:
        return StreamEvent(stream, PRESENTATION_KIND, self.event("validation").payload, key=key)

    def event(self, recipient_role_id: str) -> EventSpec:
        payload = {"version": 1, "cue_id": self.cue_id, "subject_id": self.subject_id,
                   "channel": self.channel, "phase": self.phase, "name": self.name, "data": self.data}
        validate_cue(payload)
        return EventSpec(recipient_role_id, PRESENTATION_KIND, payload)
