# Foundation audit: 0.8 to 0.9

Date: 2026-09-22. Starting commit: `79e582231e23b5f83ac501418ed734e4b1737202`.

The previous sample-world demonstrations did not establish that the foundation was complete.
This revision targets reusable world implementation and correctness under failure, not more sample gameplay.

## Reproduced defects and corrections

| Previous condition | Correction |
| --- | --- |
| A read handler could be invoked through the write core | Core rejects access-mode mismatch; read connections are physically read-only |
| SQLite context exits left connections open | Explicit commit/rollback/close, including BaseException; no ignored new-test cleanup failures |
| Concurrent cold database initialization could race | Transactional schema setup, bounded WAL setup retries, atomic secret initialization |
| HTTP and MCP validated/coerced inputs differently | One gateway, strict object schemas, finite values, bounded sizes and pages |
| Authentication and lease checks could precede a blocked write lock | Transaction-local admission and pre-commit expiry checks |
| MCP identity depended on inherited task context | Per-message request authentication and credential-bound sessions |
| Operation identity did not distinguish claimed activity targets | Target-aware replay plus a read-only receipt API |
| Read output contracts and durable event shape were unchecked | Output/event validation before atomic commit |
| State/cursor recovery had independent reads and misleading status | Snapshot-bound bootstrap, atomic event pages, explicit cursor expiry; own actions counted separately |
| Activity lifecycle lacked renewal and completion | Idempotent renew and complete/cancel with stale-proof fencing |
| Worlds depended on internal SQL tables | Scoped SDK, declared state and policies, built-in migration to portable access |
| Adding a world required loader/core edits | Trusted external `module:WORLD` definition loading |
| World upgrades lacked a coordinated data contract | Separate package/state/function versions and atomic migrations with old-process fencing |
| Imports and split services created confusing database locations | Lazy imports, installable package, one-origin launcher, included web assets |

## Verification

On the authorized Windows host, Python 3.13.5:

- 49 new unittest cases passed: 25 core/correctness, 15 world-SDK/migration, 9 real-network transport cases.
- All 12 retained regression scripts passed through `python tools/run_tests.py`.
- The historical core suite remains 31 cases; its activity retry case now requires unchanged TTL and separately checks changed-TTL conflict.
- Historical MCP tool assertions now compare the exact core-tool set plus the exact world's tools, rather than stale numeric counts.
- A wheel was built, installed into a separate target and imported outside the checkout; bundled web assets and an external world loaded successfully.
- Static undefined-name/syntax checks passed. No live model/provider was required for these deterministic tests.

Network tests use real HTTP servers and MCP SDK clients, including cross-transport replay,
credential/session mismatch, revocation, cancellation and an external turn-based rules module.
They are scripted protocol clients, not evidence of autonomous LLM behavior or every chat product's UI compatibility.

## Independent adaptations

`examples/encounter_world.py` implements participants, turns, bounded health and server rolls.
Its retry returns the same result and recorded rolls; another role recovers the state in a new session.
`examples/workflow_world.py` implements atomic task claiming and owner-only completion.
Neither contains SQL or changes the runtime. A temporary module outside the repository is also loaded in tests.
An old-format database upgrade preserves its role, credential, state version and previously committed receipt.

## Compatibility and limits

New package/contract version is 0.9.0 / 0.9, with SDK API 1; it is not an immutable 1.0 specification.
Bootstrap defaults to a bounded world view, not a full function catalog.
Strict envelopes reject previously tolerated malformed/coerced values. Activity TTL is now part of retry identity.
Authenticated MCP sessions cannot switch credentials; clients initialize a new session after token rotation.

See [the contract](FOUNDATION.md) for trust, transaction, retention and deployment boundaries.
No claim is made of arbitrary plugin sandboxing, production user-account ownership, high availability,
full D&D rule coverage or exactly-once external side effects.
