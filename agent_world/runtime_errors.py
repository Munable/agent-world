from __future__ import annotations

import json
import math


MAX_ERROR_DETAILS_BYTES = 8192


def _validate_detail_value(value, depth=0):
    if depth > 8:
        raise ValueError("error details are nested too deeply")
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("error details must contain finite JSON values")
        return
    if isinstance(value, list):
        for item in value:
            _validate_detail_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("error detail keys must be strings")
        for item in value.values():
            _validate_detail_value(item, depth + 1)
        return
    raise TypeError("error details must contain JSON values")


def _validated_details(details):
    if details is None:
        return None
    if not isinstance(details, dict):
        raise TypeError("error details must be an object")
    _validate_detail_value(details)
    encoded = json.dumps(details, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_ERROR_DETAILS_BYTES:
        raise ValueError("error details exceed the public error payload limit")
    return json.loads(encoded)


class WorldRuntimeError(RuntimeError):
    retryable = False
    recovery = None

    def __init__(
        self,
        message="",
        *,
        retryable=None,
        recovery=None,
        retry_after_seconds=None,
        details=None,
    ):
        super().__init__(message)
        if retryable is not None:
            if not isinstance(retryable, bool):
                raise TypeError("retryable must be boolean")
            self.retryable = retryable
        if recovery is not None:
            if not isinstance(recovery, str) or not recovery or len(recovery) > 128:
                raise ValueError("recovery must be a non-empty string up to 128 characters")
            self.recovery = recovery
        if retry_after_seconds is not None:
            if (
                isinstance(retry_after_seconds, bool)
                or not isinstance(retry_after_seconds, (int, float))
                or not math.isfinite(float(retry_after_seconds))
                or retry_after_seconds < 0
            ):
                raise ValueError("retry_after_seconds must be a finite non-negative number")
        self.retry_after_seconds = retry_after_seconds
        self.details = _validated_details(details)


class RegistryConflict(WorldRuntimeError):
    pass


class FunctionNotFound(WorldRuntimeError):
    recovery = "rediscover"


class FunctionVersionMismatch(WorldRuntimeError):
    recovery = "rediscover"


class SchemaRejected(WorldRuntimeError):
    recovery = "fix_request"


class OperationConflict(WorldRuntimeError):
    pass


class ActivityConflict(WorldRuntimeError):
    pass


class ActivityNotFound(WorldRuntimeError):
    pass


class ClaimBusy(WorldRuntimeError):
    pass


class ClaimFenced(WorldRuntimeError):
    pass


class CursorExpired(WorldRuntimeError):
    pass


class AuthenticationRequired(WorldRuntimeError):
    recovery = "authenticate"


class InvalidIdentityToken(WorldRuntimeError):
    recovery = "reauthenticate"


class IdentityScopeMismatch(WorldRuntimeError):
    recovery = "use_matching_credential"


class RoleNotFound(WorldRuntimeError):
    pass


class RoleInvalid(WorldRuntimeError):
    recovery = "fix_request"


class RoleInactive(WorldRuntimeError):
    pass


class JoinTicketInvalid(WorldRuntimeError):
    recovery = "obtain_new_join_ticket"


class JoinTicketExpired(WorldRuntimeError):
    recovery = "obtain_new_join_ticket"


class JoinTicketConsumed(WorldRuntimeError):
    recovery = "obtain_new_join_ticket"


class InvalidArguments(WorldRuntimeError):
    recovery = "fix_request"


class AccessModeMismatch(WorldRuntimeError):
    recovery = "use_control_credential"


class PermissionDenied(WorldRuntimeError):
    pass


class ReceiptNotFound(WorldRuntimeError):
    pass


class StateConflict(WorldRuntimeError):
    pass


class ResultRejected(WorldRuntimeError):
    pass


class WorldDefinitionError(WorldRuntimeError):
    pass


class WorldVersionMismatch(WorldRuntimeError):
    pass


class CursorInvalid(WorldRuntimeError):
    pass


class StorageBusy(WorldRuntimeError):
    retryable = True
    recovery = "retry_same_operation"


class RuleViolation(WorldRuntimeError):
    pass
