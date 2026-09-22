from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import re
from typing import Callable
from urllib.parse import urlsplit

from .runtime_contracts import identifier, integer, check_schema, validate, json_text
from .runtime_errors import WorldDefinitionError, ResultRejected, CursorInvalid

MAX_VIEW_BYTES = 98304
MAX_VIEW_ENTITIES = 256
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


class ViewNotFound(WorldDefinitionError):
    pass


class ViewResetRequired(CursorInvalid):
    pass


@dataclass(frozen=True)
class ViewSpec:
    """An optional, bounded projection for a viewer; no assumptions about a map or genre."""
    name: str
    project: Callable
    input_schema: dict = field(default_factory=lambda: dict(EMPTY))
    version: int = 1
    description: str = ""
    renderer: str = "records"
    authorize: Callable | None = None
    visible_to: Callable | None = None
    output_schema: dict | None = None
    timeline: bool = False

    def contract(self):
        identifier(self.name, "view name")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise WorldDefinitionError("invalid view name")
        integer(self.version, "view version", 1)
        identifier(self.renderer, "renderer", 128)
        if not isinstance(self.description, str) or len(self.description) > 2000:
            raise WorldDefinitionError("invalid view description")
        for callback in (self.project, self.authorize, self.visible_to):
            if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback)):
                raise WorldDefinitionError("view callbacks must be synchronous")
        if self.project is None:
            raise WorldDefinitionError("view requires a projection")
        check_schema(self.input_schema)
        if self.output_schema is not None:
            check_schema(self.output_schema)
        value = {"name": self.name, "version": self.version, "description": self.description,
                 "renderer": self.renderer, "input_schema": self.input_schema}
        if type(self.timeline) is not bool:
            raise WorldDefinitionError("timeline flag must be boolean")
        if self.timeline:
            value["timeline"] = True
        if self.output_schema is not None:
            value["output_schema"] = self.output_schema
        return value

    def validate_result(self, body):
        if inspect.isawaitable(body):
            if inspect.iscoroutine(body):
                body.close()
            raise ResultRejected("view projection must return synchronously")
        if not isinstance(body, dict) or set(body) - {"entities", "resources", "meta"}:
            raise ResultRejected("view must contain entities, resources and/or meta")
        body = {"entities": body.get("entities", {}), "resources": body.get("resources", {}),
                "meta": body.get("meta", {})}
        if not all(isinstance(body[key], dict) for key in body):
            raise ResultRejected("view sections must be objects")
        if len(body["entities"]) > MAX_VIEW_ENTITIES or len(body["resources"]) > 64:
            raise ResultRejected("view is too large; partition it with selector arguments")
        for key, entity in body["entities"].items():
            identifier(key, "entity ID")
            if not isinstance(entity, dict):
                raise ResultRejected("each view entity must be an object")
        for key, resource in body["resources"].items():
            identifier(key, "resource ID")
            if not isinstance(resource, dict) or set(resource) - {"uri", "media_type", "sha256", "size", "version"}:
                raise ResultRejected("invalid resource reference")
            uri = resource.get("uri")
            if not isinstance(uri, str) or len(uri) > 2048 or any(ord(x) < 32 for x in uri):
                raise ResultRejected("invalid resource URI")
            parsed = urlsplit(uri)
            if parsed.scheme not in {"https", "asset"} or not parsed.netloc or parsed.username or parsed.password:
                raise ResultRejected("resources require an https or asset URI without credentials")
            identifier(resource.get("media_type"), "resource media type")
            if "sha256" in resource and not re.fullmatch(r"[a-f0-9]{64}", str(resource["sha256"])):
                raise ResultRejected("invalid resource digest")
            if "size" in resource:
                integer(resource["size"], "resource size")
            if "version" in resource:
                identifier(resource["version"], "resource version")
        json_text(body, maximum=MAX_VIEW_BYTES)
        if self.output_schema is not None:
            validate(self.output_schema, body, maximum=MAX_VIEW_BYTES)
        return body


def diff_view(before, after):
    result = {}
    for section in ("entities", "resources"):
        old, new = before[section], after[section]
        result[section] = {"upsert": {k: v for k, v in new.items() if k not in old or old[k] != v},
                           "remove": sorted(set(old) - set(new))}
    result["meta"] = after["meta"]
    return result
