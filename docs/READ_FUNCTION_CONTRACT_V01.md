# Read Function Contract v0.1

> Historical design note. Current execution, identity and versioning rules are defined in [Foundation 0.9](FOUNDATION.md).

Date: 2026-09-21

Function Registry access is now executable semantics, not metadata only.

## Write functions

- access = write
- require operation_id
- use explicit idempotency
- execute in transactional mutation path
- may change world_state
- may emit durable events
- produce operation receipts

## Read functions

- access = read
- do not expose or require operation_id
- do not create operation receipts
- do not emit durable events
- do not require activity claims in v0.6
- execute on a database connection with PRAGMA query_only=ON
- FunctionContext.set_state rejects writes explicitly
A read handler that tries either ctx.set_state() or direct SQL mutation fails.

The read result shape is:

- ok
- function_id
- function_version
- result
- read_only = true

HTTP and MCP derive their contracts from the same Function Registry descriptor.

Authenticated MCP therefore exposes:

- no role_id for either reads or writes
- operation_id only for write world functions

This keeps transport authentication, query semantics, and mutation
idempotency separate instead of encoding them in prompt prose.
