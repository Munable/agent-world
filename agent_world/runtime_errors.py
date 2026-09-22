class WorldRuntimeError(RuntimeError):
    retryable = False


class RegistryConflict(WorldRuntimeError):
    pass


class FunctionNotFound(WorldRuntimeError):
    pass


class FunctionVersionMismatch(WorldRuntimeError):
    pass


class SchemaRejected(WorldRuntimeError):
    pass


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
    pass


class InvalidIdentityToken(WorldRuntimeError):
    pass


class IdentityScopeMismatch(WorldRuntimeError):
    pass


class RoleNotFound(WorldRuntimeError):
    pass


class RoleInvalid(WorldRuntimeError):
    pass


class RoleInactive(WorldRuntimeError):
    pass


class JoinTicketInvalid(WorldRuntimeError):
    pass


class JoinTicketExpired(WorldRuntimeError):
    pass


class JoinTicketConsumed(WorldRuntimeError):
    pass


class InvalidArguments(WorldRuntimeError):
    pass


class AccessModeMismatch(WorldRuntimeError):
    pass


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


class RuleViolation(WorldRuntimeError):
    pass
