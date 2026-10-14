"""Public, user-owned Shared Grove client independent of a model or Agent host.

World capability discovery, fixed presented observations and original-operation
recovery are shared by HTTP/MCP consumers. The caller owns inference, intent,
credential storage, authorization, budgets and cancellation.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import hashlib
import json
import secrets

from jsonschema import Draft202012Validator

from .grove_agent_errors import AgentClientError
from .grove_agent_observation import AgentObservations, agent_mcp_schema
from .grove_agent_ports import (GroveTransport, HttpGroveTransport, McpGroveTransport,
    OperationJournal, MemoryOperationJournal, JsonOperationJournal, CallbackOperationJournal)


_OBSERVATION_PROTOCOL = {"type": "object", "properties": {
    "version": {"const": 1}, "scope": {"const": "client_presented"},
    "world_instance": {"type": "string"}, "role_id": {"type": "string"},
    "source_function": {"type": "string"},
    "source_kind": {"enum": ["query", "receipt", "bootstrap", "host_readback"]},
    "semantics": {"type": "object", "properties": {
        "current": {"const": "Current reads do not replace historical receipts."},
        "historical": {"const": "A receipt proves its original committed operation, not a goal."},
        "declaration": {"const": "Authored definitions do not prove instances, resources or effects."},
        "eligibility": {"const": "Observed availability is rechecked by the original Command."}},
        "required": ["current", "historical", "declaration", "eligibility"], "additionalProperties": False}},
    "required": ["version", "scope", "world_instance", "role_id", "source_function", "source_kind", "semantics"],
    "additionalProperties": False}
_RESULT_SHAPE = {"type": "object", "properties": {
    **{name: {"type": ["object", "null"]} for name in ("player", "entity", "viewport", "public_content", "agent_context")},
    **{name: {"type": "array", "items": {"type": "object"}} for name in ("entities", "inventory", "contents", "action_options", "read_requests")},
    "available_actions": {"type": "array", "items": {"type": "string"}},
    "projection": {"type": "object"}}, "additionalProperties": True}
_INTENT_FINGERPRINT_ALGORITHM = "sha256-runtime-canonical-json-v1"


def observation_output_schema():
    """Schema of the SDK projection, deliberately not the raw world result."""
    return {"type": "object", "properties": {
        "observation_id": {"type": "string", "minLength": 1},
        "agent_observation": deepcopy(_OBSERVATION_PROTOCOL),
        "result": deepcopy(_RESULT_SHAPE), "execution_readback": deepcopy(_RESULT_SHAPE),
        "operation_id": {"type": "string"}, "function_id": {"type": "string"},
        "function_version": {"type": "integer", "minimum": 1},
        "commit_seq": {"type": "integer", "minimum": 0}, "snapshot_seq": {"type": "integer", "minimum": 0},
        "ok": {"type": "boolean"}, "read_only": {"type": "boolean"},
        "replayed": {"type": "boolean"},
        "intent_fingerprint": {"type": "object", "properties": {
            "algorithm": {"const": _INTENT_FINGERPRINT_ALGORITHM},
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "required": ["algorithm", "digest"], "additionalProperties": False}},
        "required": ["observation_id", "agent_observation"], "additionalProperties": True}


def mcp_tool_output_schema():
    """Public MCP structured content may be a successful read or a failure.

    MCP clients validate structuredContent even when isError is true. Errors
    carry no new observation; successful presentation still validates against
    observation_output_schema directly before reaching this tool boundary.
    """
    failure = {"type": "object", "properties": {
        "error": {"type": "string", "minLength": 1}, "message": {"type": "string"},
        "retryable": {"type": "boolean"}, "recovery": {"type": "string"},
        "retry_after_seconds": {"type": "number", "minimum": 0},
        "details": {"type": "object"}, "execution_outcome": {"type": "object"},
        "observation_id": False, "agent_observation": False},
        "required": ["error", "message"], "additionalProperties": True}
    return {"type": "object", "anyOf": [observation_output_schema(), failure]}


def _client_error(exc):
    if isinstance(exc, AgentClientError):
        return exc
    if hasattr(exc, "code") and hasattr(exc, "status"):
        return AgentClientError(exc.code, str(exc), exc.status,
            world_details=getattr(exc, "world_details", None), retryable=getattr(exc, "retryable", None),
            recovery=getattr(exc, "recovery", None), retry_after_seconds=getattr(exc, "retry_after_seconds", None))
    return AgentClientError("WorldUnavailable", "Original request outcome is unavailable.", 503,
                            recovery="query_receipt_before_retry")


class GroveAgentClient:
    def __init__(self, transport: GroveTransport, role_id: str, world_instance: str, *,
                 journal: OperationJournal, observations: AgentObservations | None = None,
                 before_write=None):
        if not isinstance(role_id, str) or not role_id or not isinstance(world_instance, str) or not world_instance:
            raise ValueError("role_id and world_instance must identify the authenticated endpoint")
        self.transport, self.role_id, self.world_instance = transport, role_id, world_instance
        self.journal, self.before_write = journal, before_write
        self.observations = observations or AgentObservations(role_id)
        if self.observations.role_id != role_id:
            raise ValueError("observations belong to another role")
        self.catalog = {}
        self.contract = None
        self._functions = {}
        self._write_lock = asyncio.Lock()
        self._persistence_failed = False
        self._world_scope = None
        self._world_id = None
        self._world_version = None
        self._latest_snapshot = None
        self._catalog_current = False
        try:
            self._pending = self._validate_records(journal.load())
            if self._pending:
                self._world_scope = self._pending[0]["universe"]
        except Exception as exc:
            raise AgentClientError("CorruptJournal", "Keep the original journal; no writes were admitted.", 500) from exc

    def _validate_records(self, records):
        if not isinstance(records, list):
            raise ValueError("journal must be a list")
        seen = set()
        universes = set()
        for row in records:
            if not isinstance(row, dict) or row.get("world_instance") != self.world_instance or row.get("role_id") != self.role_id:
                raise ValueError("journal scope mismatch")
            universe = row.get("universe")
            if (not isinstance(universe, str) or not universe
                    or self._world_scope is not None and universe != self._world_scope):
                raise ValueError("journal universe mismatch")
            universes.add(universe)
            if len(universes) != 1:
                raise ValueError("journal spans multiple universes")
            envelope = row.get("envelope")
            if not isinstance(row.get("function_id"), str) or not isinstance(envelope, dict):
                raise ValueError("journal operation missing")
            identity = envelope.get("operation_id")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or identity in seen:
                raise ValueError("journal identity invalid")
            if not isinstance(envelope.get("arguments"), dict):
                raise ValueError("journal arguments missing")
            if row.get("envelope_fingerprint") != self._fingerprint(row["function_id"], envelope, universe):
                raise ValueError("original journal envelope changed")
            seen.add(identity)
        return deepcopy(records)

    def _fingerprint(self, function, envelope, universe):
        canonical = json.dumps([self.world_instance, self.role_id, universe, function, envelope], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def operation_record(self, function, envelope):
        """Bind an original envelope to authenticated scope, without Agent tool admission.

        Host-owned control Commands may use this journal/receipt contract while
        retaining their own authorization and never entering the Agent menu.
        """
        if self._world_scope is None:
            raise AgentClientError("AgentDiscoveryRequired", "Authenticate the world scope before recording an operation.", 409)
        record = {"world_instance": self.world_instance, "role_id": self.role_id, "universe": self._world_scope,
                  "function_id": function, "envelope": deepcopy(envelope),
                  "envelope_fingerprint": self._fingerprint(function, envelope, self._world_scope)}
        self.validate_operation_record(record)
        return record

    def validate_operation_record(self, record):
        """Validate a host's original durable record; legacy unscoped rows stay closed."""
        try:
            self._validate_records([record])
        except (ValueError, TypeError, KeyError) as exc:
            raise AgentClientError("OriginalOperationInvalid", "Original operation scope or envelope is unverified; preserve the journal.", 409,
                                   recovery="preserve_original_journal") from exc

    def validate_operation_scope(self, bootstrap, *, record=None, recovery=False):
        """Check real bootstrap before receipt lookup or original-envelope send."""
        if record is not None:
            self.validate_operation_record(record)
            if not isinstance(bootstrap, dict) or bootstrap.get("universe") != record["universe"]:
                raise AgentClientError("IdentityScopeMismatch", "Original operation belongs to another world instance; preserve the journal.", 409)
        self._check_bootstrap_scope(bootstrap, recovery=recovery or self._world_id is None)
        if self._world_scope is None:
            # Establish identity only. This never installs or activates tools.
            self._world_scope = bootstrap["universe"]
            self._world_id, self._world_version = bootstrap["world"]["id"], bootstrap["world"]["version"]

    def validate_receipt(self, payload, record):
        """Verify original intent evidence without discovering tools or admitting a write."""
        self._check_receipt(payload, record)

    @staticmethod
    def _intent_fingerprint(function, arguments):
        canonical = json.dumps({"function_id": function, "arguments": arguments}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"algorithm": _INTENT_FINGERPRINT_ALGORITHM,
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}

    @property
    def pending_operations(self):
        return deepcopy(self._pending)

    @property
    def function_ids(self):
        return tuple(self._functions)

    def function_contract(self, function):
        return deepcopy(self._functions.get(function, {}))

    def _save(self):
        try:
            self.journal.save(deepcopy(self._pending))
        except Exception as exc:
            self._persistence_failed = True
            raise AgentClientError("JournalUnavailable", "Operation persistence failed; further writes are closed.", 500,
                                   recovery="repair_journal_then_recover_original_operations") from exc

    def configure_catalog(self, functions, contract, *, activate=True):
        """Install an explicitly supplied world contract, never inferred prose."""
        if not isinstance(contract, dict) or contract.get("contract_version") != 1:
            raise AgentClientError("AgentContractUnsupported", "Rediscover a supported Agent contract.", 409,
                                   recovery="rediscover")
        if self._world_id is not None and contract.get("world_id") != self._world_id:
            raise AgentClientError("InvalidAgentContract", "Agent contract belongs to another world code.", 409,
                                   recovery="rediscover")
        catalog = {row["function_id"]: deepcopy(row) for row in functions}
        selected = {}
        names = set()
        for item in contract.get("functions", []):
            if not isinstance(item, dict) or not isinstance(item.get("function_id"), str):
                raise AgentClientError("InvalidAgentContract", "Agent function declarations must be structured.", 502)
            function = item["function_id"]
            descriptor = catalog.get(function)
            if descriptor is None or item.get("access") not in ("read", "write") or descriptor.get("access") != item["access"]:
                raise AgentClientError("InvalidAgentContract", "Agent capability and discovered function disagree.", 409,
                                       recovery="rediscover")
            name = function.replace(".", "_")
            if name in names:
                raise AgentClientError("InvalidAgentContract", "Agent tool names must be distinct.", 502)
            names.add(name)
            selected[function] = deepcopy(item)
        self.catalog, self.contract, self._functions = catalog, deepcopy(contract), selected
        if activate:
            self._catalog_current = True

    def _check_bootstrap_scope(self, bootstrap, *, rediscover=False, recovery=False):
        if not isinstance(bootstrap, dict) or bootstrap.get("role_id") != self.role_id:
            raise AgentClientError("IdentityScopeMismatch", "Bootstrap belongs to another role.", 403)
        universe, world = bootstrap.get("universe"), bootstrap.get("world")
        if not isinstance(universe, str) or not universe or not isinstance(world, dict):
            raise AgentClientError("IdentityScopeMismatch", "Bootstrap did not identify this world instance.", 409)
        if self._world_scope is not None and universe != self._world_scope:
            raise AgentClientError("IdentityScopeMismatch", "Transport points to another world instance; preserve the original journal.", 409)
        code, version = world.get("id"), world.get("version")
        if not isinstance(code, str) or not code or type(version) is not int or version < 1:
            raise AgentClientError("AgentOutputMismatch", "Bootstrap world code and version are invalid.", 502)
        if rediscover:
            self._world_scope, self._world_id, self._world_version = universe, code, version
        elif not recovery and (code != self._world_id or version != self._world_version):
            self._catalog_current = False
            raise AgentClientError("WorldVersionMismatch", "World code changed; rediscover before a new write.", 409,
                                   recovery="rediscover")

    async def _check_live_scope(self, *, recovery=False):
        try:
            bootstrap = await self.transport.bootstrap()
        except Exception as exc:
            raise _client_error(exc) from exc
        self._check_bootstrap_scope(bootstrap, recovery=recovery)

    async def discover(self):
        """Follow bootstrap's formal contract request and paged live schemas."""
        self._catalog_current = False
        bootstrap = await self.transport.bootstrap()
        self._check_bootstrap_scope(bootstrap, rediscover=True)
        state = bootstrap.get("world_entry_state", {})
        view = state.get("view", {}) if isinstance(state, dict) else {}
        request = view.get("agent_contract_request") if isinstance(view, dict) else None
        if not isinstance(request, dict) or not isinstance(request.get("function_id"), str) or not isinstance(request.get("arguments", {}), dict):
            raise AgentClientError("AgentContractRequired", "World bootstrap did not declare an Agent contract request.", 409,
                                   recovery="rediscover")
        rows, after, cursors = [], "", set()
        while True:
            page = await self.transport.discover(after=after, include_schemas=True)
            rows.extend(page.get("functions", []))
            if not page.get("has_more"):
                break
            after = page.get("next_cursor")
            if not isinstance(after, str) or not after or after in cursors:
                raise AgentClientError("InvalidDiscovery", "World returned a non-progressing discovery cursor.", 502)
            cursors.add(after)
        descriptor = next((row for row in rows if row.get("function_id") == request["function_id"]), None)
        if descriptor is None or descriptor.get("access") != "read":
            raise AgentClientError("InvalidAgentContract", "Agent contract must be a discovered read-only function.", 409)
        payload = await self.transport.invoke(request["function_id"], {"arguments": deepcopy(request.get("arguments", {}))})
        self._check_query(payload, descriptor)
        self.configure_catalog(rows, payload.get("result", payload))
        observed = self.present("world_bootstrap", bootstrap, preflight=True)
        self._latest_snapshot = payload["snapshot_seq"]
        return {"bootstrap": deepcopy(bootstrap), "bootstrap_observation": observed,
                "observation_id": observed["observation_id"], "contract": deepcopy(self.contract), "functions": deepcopy(rows)}

    def _spec(self, function, access=None):
        if function not in self._functions:
            raise AgentClientError("ToolDenied", "Function is outside this world's declared Agent interface.", 403)
        spec = self.catalog[function]
        if access is not None and spec.get("access") != access:
            raise AgentClientError("ToolDenied", "Function access does not match this operation.", 403)
        return spec

    def tool_schema(self, function):
        spec = self._spec(function)
        return agent_mcp_schema(spec["input_schema"], function=function, access=spec["access"],
                                guard_paths=self._functions[function].get("guard_paths"))

    def tools(self):
        return [{"name": function.replace(".", "_"), "description": self.catalog[function].get("description", ""),
                 "inputSchema": self.tool_schema(function), "outputSchema": mcp_tool_output_schema()}
                for function in sorted(self._functions)]

    def function_for_tool(self, name):
        return next((function for function in self._functions if function.replace(".", "_") == name), None)

    def present(self, function, payload, **kwargs):
        try:
            kwargs.setdefault("schema_projector", self._menu_schema)
            delivered = self.observations.present(function, payload, **kwargs)
            kind = ("bootstrap" if function == "world_bootstrap" else "receipt" if "operation_id" in payload
                    else "host_readback" if function == "execution_checkpoint" else "query")
            delivered["agent_observation"] = {"version": 1, "scope": "client_presented",
                "world_instance": self.world_instance, "role_id": self.role_id, "source_function": function,
                "source_kind": kind, "semantics": {name: value["const"] for name, value in
                    _OBSERVATION_PROTOCOL["properties"]["semantics"]["properties"].items()}}
            self._validate_output(observation_output_schema(), delivered)
            # Verify the complete projected body, including protocol metadata,
            # against the same native limit before the caller can use it.
            self.observations.serialize(delivered)
            return delivered
        except Exception as exc:
            if hasattr(exc, "code"):
                raise _client_error(exc) from exc
            raise

    def _menu_schema(self, function, schema):
        item = self._functions.get(function, {})
        return agent_mcp_schema(schema, function=function, access=item.get("access"),
                                guard_paths=item.get("guard_paths"))

    def validate_bootstrap(self, payload):
        """Validate an adapter's authenticated bootstrap without discovering tools."""
        self._check_bootstrap_scope(payload, recovery=self._world_id is None)

    def validate_query(self, function, payload, *, accept_watermark=False):
        """Validate a raw world Query before an adapter uses or projects its facts."""
        self._check_query(payload, self._spec(function, "read"))
        if accept_watermark:
            self._latest_snapshot = payload["snapshot_seq"]

    def present_query(self, function, payload, **kwargs):
        """Present only a validated raw Query; host composites use present()."""
        self.validate_query(function, payload)
        delivered = self.present(function, payload, **kwargs)
        self._latest_snapshot = payload["snapshot_seq"]
        return delivered

    def _check_query(self, payload, spec):
        if (not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("read_only") is not True
                or payload.get("function_id") != spec["function_id"]):
            raise AgentClientError("AgentOutputMismatch", "Query response does not match its discovered function.", 502)
        if type(spec.get("version")) is int and (type(payload.get("function_version")) is not int
                or payload.get("function_version") != spec["version"]):
            self._catalog_current = False
            raise AgentClientError("AgentOutputMismatch", "Query function version changed; rediscover.", 409, recovery="rediscover")
        result = payload.get("result")
        context = result.get("agent_context") if isinstance(result, dict) else None
        if isinstance(context, dict):
            if (context.get("viewer_role_id") != self.role_id
                    or context.get("world_instance") != self._world_scope
                    or type(context.get("snapshot_seq")) is not int
                    or context.get("snapshot_seq") != payload.get("snapshot_seq")):
                raise AgentClientError("IdentityScopeMismatch", "Query context does not match this world, role and snapshot.", 409)
            if (context.get("world_id") != self._world_id or type(context.get("world_version")) is not int
                    or context.get("world_version") != self._world_version):
                self._catalog_current = False
                raise AgentClientError("WorldVersionMismatch", "Query world code changed; rediscover before a new write.", 409,
                                       recovery="rediscover")
        if spec.get("output_schema") is not None:
            self._validate_output(spec["output_schema"], result)
        snapshot = payload.get("snapshot_seq")
        if type(snapshot) is not int or snapshot < 0:
            raise AgentClientError("AgentOutputMismatch", "Query snapshot sequence is invalid.", 502)
        if self._latest_snapshot is not None and snapshot < self._latest_snapshot:
            raise AgentClientError("StaleObservation", "An older read cannot replace newer presented facts; read again.", 409,
                                   recovery="read_again")

    @staticmethod
    def _validate_output(schema, value):
        if not Draft202012Validator(schema).is_valid(value):
            raise AgentClientError("AgentOutputMismatch", "Response does not match the declared output schema.", 502)

    def bind(self, function, arguments, observation_id):
        if not self._catalog_current:
            raise AgentClientError("AgentDiscoveryRequired", "Rediscover the world contract before binding a new write.", 409,
                                   recovery="rediscover")
        spec = self._spec(function, "write")
        try:
            return self.observations.bind(function, arguments, observation_id, spec["input_schema"],
                guard_paths=self._functions[function].get("guard_paths"))
        except Exception as exc:
            if hasattr(exc, "code"):
                raise _client_error(exc) from exc
            raise

    async def read(self, function, arguments=None, *, base_observation_id=None, preflight=False):
        spec = self._spec(function, "read")
        arguments = deepcopy(arguments or {})
        self._validate(spec["input_schema"], arguments)
        if base_observation_id is not None:
            self.observations.get(base_observation_id)
        envelope = {"arguments": arguments}
        if type(spec.get("version")) is int:
            envelope["expected_version"] = spec["version"]
        try:
            payload = await self.transport.invoke(function, envelope)
        except Exception as exc:
            raise _client_error(exc) from exc
        return self.present_query(function, payload, base_id=base_observation_id, preflight=preflight)

    @staticmethod
    def _validate(schema, arguments):
        errors = list(Draft202012Validator(schema).iter_errors(arguments))
        if errors:
            paths = [".".join(str(item) for item in error.absolute_path) or "$" for error in errors[:3]]
            raise AgentClientError("InvalidToolArguments", "Input does not match the discovered schema at " + ", ".join(paths), 422)

    async def _admit(self, record):
        if self.before_write is not None:
            result = self.before_write(deepcopy(record))
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise AgentClientError("WriteDenied", "The host declined this original operation.", 403)

    async def write(self, function, arguments, *, observation_id, operation_id=None):
        """One new intent; never replace a missing observation or unknown write."""
        async with self._write_lock:
            if self._persistence_failed:
                raise AgentClientError("JournalUnavailable", "Further writes are closed until journal recovery.", 500)
            if self._pending:
                raise AgentClientError("UnknownWrite", "Recover original operations before a new write.", 409,
                                       recovery="query_receipt_before_retry")
            if not self._catalog_current or self._world_scope is None:
                raise AgentClientError("AgentDiscoveryRequired", "Rediscover the world contract before a new write.", 409,
                                       recovery="rediscover")
            await self._check_live_scope()
            spec = self._spec(function, "write")
            bound = self.bind(function, deepcopy(arguments), observation_id)
            self._validate(spec["input_schema"], bound)
            identity = operation_id or "grove-agent-" + secrets.token_hex(16)
            if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                raise AgentClientError("InvalidArguments", "operation_id must contain 1 to 128 characters.", 422)
            envelope = {"operation_id": identity, "arguments": bound}
            if type(spec.get("version")) is int:
                envelope["expected_version"] = spec["version"]
            record = self.operation_record(function, envelope)
            await self._admit(record)
            self._pending.append(deepcopy(record))
            self._save()  # Durable original envelope precedes every send.
            try:
                payload = await self.transport.invoke(function, deepcopy(envelope))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = _client_error(exc)
                if self._definite_rejection(error):
                    self._settle(record)
                raise error from exc
            try:
                self._check_receipt(payload, record)
            except AgentClientError as exc:
                if exc.code == "OperationConflict":
                    self._settle(record)
                raise
            self._settle(record)
            try:
                return self.present(function, payload, base_id=observation_id)
            except Exception as exc:
                raise AgentClientError("ReadbackPending", "Original action committed; recover its receipt before another decision.", 409,
                    world_details={"operation_id": identity, "function_id": function, "operation_committed": True},
                    recovery="query_receipt_before_retry") from exc

    @staticmethod
    def _definite_rejection(error):
        # Status approximation alone is insufficient on MCP or a proxy. These
        # are explicit Runtime refusals, never an unknown transport outcome.
        if error.code == "DomainBusy":
            return (error.world_details or {}).get("settlement", {}).get("operation_committed") is False
        return error.code in {"PermissionDenied", "AuthenticationRequired", "IdentityScopeMismatch", "RuleViolation",
            "InvalidArguments", "SchemaRejected", "VersionConflict", "StateConflict", "OperationConflict", "FunctionNotFound",
            "ResourceNotFound", "RoleInactive", "AccessModeMismatch", "ActivityClaimRequired", "ActivityClaimExpired",
            "FunctionVersionMismatch", "WorldVersionMismatch"}

    def _settle(self, record):
        original = self._pending
        self._pending = [item for item in original if item != record]
        try:
            self._save()
        except Exception:
            self._pending = original
            raise

    def _check_receipt(self, payload, record):
        self.validate_operation_record(record)
        if (not isinstance(payload, dict) or payload.get("ok") is not True
                or payload.get("operation_id") != record["envelope"]["operation_id"]
                or payload.get("actor_role_id") != self.role_id
                or not isinstance(payload.get("function_id"), str) or not payload["function_id"]
                or type(payload.get("function_version")) is not int or payload["function_version"] < 1
                or type(payload.get("commit_seq")) is not int or payload["commit_seq"] < 0
                or not isinstance(payload.get("result"), dict)):
            raise AgentClientError("ReceiptMismatch", "Original write receipt could not be verified; preserve the journal.", 409,
                                   recovery="query_receipt_before_retry")
        proof = payload.get("intent_fingerprint")
        digest = proof.get("digest") if isinstance(proof, dict) else None
        if (not isinstance(proof, dict) or set(proof) != {"algorithm", "digest"}
                or proof.get("algorithm") != _INTENT_FINGERPRINT_ALGORITHM
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise AgentClientError("ReceiptMismatch", "Receipt lacks the original intent's verifiable fingerprint; preserve the journal.", 409,
                                   recovery="query_receipt_before_retry")
        if proof != self._intent_fingerprint(record["function_id"], record["envelope"]["arguments"]):
            # IDs are unique within the authenticated universe and actor. A
            # valid receipt for a different intent proves this intent could
            # not commit under that ID, even if its refusal reply was lost.
            raise AgentClientError("OperationConflict", "This operation ID belongs to a different committed intent.", 409,
                                   world_details={"operation_id": record["envelope"]["operation_id"],
                                                  "operation_committed": False})
        if (payload["function_id"] != record["function_id"]
                or "expected_version" in record["envelope"]
                and payload["function_version"] != record["envelope"]["expected_version"]):
            raise AgentClientError("ReceiptMismatch", "Receipt function does not match its intent evidence; preserve the journal.", 409,
                                   recovery="query_receipt_before_retry")

    async def recover(self, *, replay=False):
        """Receipt first; replay is explicit and always retains the exact envelope."""
        async with self._write_lock:
            if self._pending:
                # Receipt IDs are scoped to the original authenticated world.
                # Recovery permits changed code/catalog, but never another
                # universe or actor and never a newly bound observation.
                await self._check_live_scope(recovery=True)
            # Explicit recovery first proves that the original records can
            # again be saved. Pending operations still block every new intent.
            self._save()
            self._persistence_failed = False
            results = []
            for record in list(self._pending):
                self._validate_records([record])
                needs_replay = False
                try:
                    payload = await self.transport.receipt(record["envelope"]["operation_id"])
                except Exception as exc:
                    error = _client_error(exc)
                    if error.code != "ReceiptNotFound":
                        raise error from exc
                    if not replay:
                        results.append({"operation_id": record["envelope"]["operation_id"], "status": "unknown"})
                        continue
                    needs_replay = True
                if not needs_replay:
                    try:
                        self._check_receipt(payload, record)
                    except AgentClientError as proof_error:
                        if proof_error.code == "OperationConflict":
                            self._settle(record)
                            results.append({"operation_id": record["envelope"]["operation_id"],
                                            "status": "rejected", "error": proof_error.public_payload()})
                            continue
                        if not replay or proof_error.code != "ReceiptMismatch":
                            raise
                        needs_replay = True
                if needs_replay:
                    if self._persistence_failed:
                        raise AgentClientError("JournalUnavailable", "Replay remains closed until persistence is repaired.", 500)
                    # Missing receipt/evidence is never a confirmed result.
                    # Explicit replay sends only the persisted original ID and
                    # still requires the host's current write authorization.
                    await self._admit(record)
                    self._save()
                    try:
                        payload = await self.transport.invoke(record["function_id"], deepcopy(record["envelope"]))
                    except Exception as replay_exc:
                        replay_error = _client_error(replay_exc)
                        if self._definite_rejection(replay_error):
                            self._settle(record)
                            results.append({"operation_id": record["envelope"]["operation_id"],
                                            "status": "rejected", "error": replay_error.public_payload()})
                            continue
                        raise replay_error from replay_exc
                    try:
                        self._check_receipt(payload, record)
                    except AgentClientError as proof_error:
                        if proof_error.code == "OperationConflict":
                            self._settle(record)
                            results.append({"operation_id": record["envelope"]["operation_id"],
                                            "status": "rejected", "error": proof_error.public_payload()})
                            continue
                        raise
                self._settle(record)
                results.append({"operation_id": record["envelope"]["operation_id"],
                                "status": "committed", "receipt": deepcopy(payload)})
            if not self._pending:
                self._save()
                self._persistence_failed = False
            return results


__all__ = ["GroveAgentClient", "AgentClientError", "GroveTransport", "HttpGroveTransport", "McpGroveTransport",
           "OperationJournal", "MemoryOperationJournal", "JsonOperationJournal", "CallbackOperationJournal",
           "observation_output_schema", "mcp_tool_output_schema"]
