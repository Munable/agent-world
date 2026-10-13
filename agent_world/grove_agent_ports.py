"""Replaceable world transports and operation journals; no Agent host control."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .grove_agent_errors import AgentClientError


class GroveTransport(Protocol):
    async def bootstrap(self) -> dict: ...
    async def discover(self, *, after="", limit=50, include_schemas=True) -> dict: ...
    async def invoke(self, function_id: str, envelope: dict) -> dict: ...
    async def receipt(self, operation_id: str) -> dict: ...


class HttpGroveTransport:
    """Use an existing credential; identity proof and host policy stay outside."""
    def __init__(self, world_url: str, bearer: str = "", *, request: Callable | None = None):
        parsed = urlsplit(world_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("world_url must be an HTTP endpoint without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("world_url cannot contain a query or fragment")
        self.world_url, self._bearer, self._request = world_url.rstrip("/"), bearer, request

    async def _call(self, method, path, body=None):
        if self._request is not None:
            return await self._request(method, path, body)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                response = await client.request(method, self.world_url + path,
                    headers={"Authorization": "Bearer " + self._bearer}, json=body)
        except httpx.HTTPError:
            raise AgentClientError("WorldUnavailable", "World request outcome is unavailable.", 503,
                                   recovery="query_receipt_before_retry") from None
        try:
            payload = response.json()
        except ValueError:
            raise AgentClientError("InvalidWorldResponse", "World returned an invalid response.", 502) from None
        if not isinstance(payload, dict):
            raise AgentClientError("InvalidWorldResponse", "World response must be an object.", 502)
        if response.status_code >= 400:
            raise AgentClientError(payload.get("error", "WorldError"), payload.get("message", "World request failed."),
                response.status_code, world_details=payload.get("details"), retryable=payload.get("retryable"),
                recovery=payload.get("recovery"), retry_after_seconds=payload.get("retry_after_seconds"))
        if not response.is_success:
            raise AgentClientError("WorldRedirectRefused", "World redirects are not accepted.", 502)
        return payload

    async def bootstrap(self):
        return await self._call("GET", "/v1/bootstrap")

    async def discover(self, *, after="", limit=50, include_schemas=True):
        query = urlencode({"after": after, "limit": limit, "include_schemas": str(include_schemas).lower()})
        return await self._call("GET", "/v1/discover?" + query)

    async def invoke(self, function_id, envelope):
        return await self._call("POST", "/v1/functions/" + quote(function_id, safe=".") + "/invoke", deepcopy(envelope))

    async def receipt(self, operation_id):
        return await self._call("GET", "/v1/receipts/" + quote(operation_id, safe=""))


class McpGroveTransport:
    """Adapt a caller-owned initialized MCP session or generic call_tool port."""
    def __init__(self, call_tool: Callable):
        self._call_tool = call_tool

    async def _call(self, name, arguments):
        result = await self._call_tool(name, deepcopy(arguments))
        if isinstance(result, dict):
            payload = result.get("structuredContent", result)
            failed = result.get("isError", False)
        else:
            payload = getattr(result, "structured_content", None)
            failed = getattr(result, "is_error", False)
        if not isinstance(payload, dict):
            raise AgentClientError("InvalidWorldResponse", "MCP structuredContent must be an object.", 502)
        if failed or "error" in payload:
            # A core error code determines semantics; HTTP status is only a
            # diagnostic approximation on this transport.
            code = payload.get("error", "WorldError")
            status = {"ReceiptNotFound": 404, "ResourceNotFound": 404, "PermissionDenied": 403,
                "AuthenticationRequired": 401, "DomainBusy": 409, "VersionConflict": 409,
                "StateConflict": 409, "FunctionVersionMismatch": 409, "WorldVersionMismatch": 409,
                "OperationConflict": 409, "InvalidArguments": 422, "RuleViolation": 409,
                "SchemaRejected": 422}.get(code, 500)
            raise AgentClientError(code, payload.get("message", "World request failed."), status,
                world_details=payload.get("details"), retryable=payload.get("retryable"),
                recovery=payload.get("recovery"), retry_after_seconds=payload.get("retry_after_seconds"))
        return deepcopy(payload)

    async def bootstrap(self):
        return await self._call("world.bootstrap", {})

    async def discover(self, *, after="", limit=50, include_schemas=True):
        return await self._call("world.discover", {"after": after, "limit": limit, "include_schemas": include_schemas})

    async def invoke(self, function_id, envelope):
        return await self._call(function_id, envelope)

    async def receipt(self, operation_id):
        return await self._call("world.get_receipt", {"operation_id": operation_id})


class OperationJournal(Protocol):
    def load(self) -> list[dict]: ...
    def save(self, records: list[dict]) -> None: ...


class MemoryOperationJournal:
    """An explicit non-durable journal for callers that do not need restart recovery."""
    def __init__(self):
        self._records = []

    def load(self):
        return deepcopy(self._records)

    def save(self, records):
        self._records = deepcopy(records)


class CallbackOperationJournal:
    def __init__(self, load: Callable, save: Callable):
        self._load, self._save = load, save

    def load(self):
        return deepcopy(self._load())

    def save(self, records):
        self._save(deepcopy(records))


class JsonOperationJournal:
    """Persist only original operations at a caller-selected stable path."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.recovery_path = self.path.with_name(self.path.name + ".recovery")

    def load(self):
        if not self.path.exists() and not self.recovery_path.exists():
            return []
        try:
            current = json.loads(self.path.read_text()) if self.path.exists() else []
            previous = json.loads(self.recovery_path.read_text()) if self.recovery_path.exists() else []
        except (OSError, ValueError):
            raise AgentClientError("CorruptJournal", "Keep the original operation journal for recovery.", 500) from None
        if not isinstance(current, list) or not isinstance(previous, list):
            raise AgentClientError("CorruptJournal", "Operation journal must be a list.", 500)
        records = deepcopy(current)
        for row in previous:
            if row not in records:
                records.append(deepcopy(row))
        return records

    def save(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp-" + secrets.token_hex(4))
        try:
            # Keep one original journal until the replacement has crossed the
            # directory durability boundary. A failed clear must not erase the
            # only recovery identity before the caller receives its result.
            if self.path.exists() and not self.recovery_path.exists():
                os.link(self.path, self.recovery_path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(records, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # A returned save includes directory durability, not only rename.
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.recovery_path.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
