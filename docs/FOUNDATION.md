# Foundation contract 0.10

This is a versioned development contract, not a claim of production completeness.
The runtime owns transport, identity admission, transactions, receipts and event cursors.
A world package owns its rules, data schemas, authorization policy and recovery view.

## Minimal external world

```python
from agent_world import WorldDefinition, FunctionSpec, StateRule, FunctionOutcome

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

def advance(ctx, arguments):
    record = ctx.get_state_record("world", "count", 0)
    value = record["value"] + 1
    ctx.set_state("world", "count", value, expected_version=record["version"])
    return FunctionOutcome({"value": value})

def snapshot(ctx):
    return {"count": ctx.get_state("world", "count", 0)}

WORLD = WorldDefinition(
    world_id="counter-world", display_name="Counter World",
    functions=(FunctionSpec("counter.advance", advance, EMPTY),),
    state_rules=(StateRule("world", "count", {"type": "integer"}),),
    bootstrap=snapshot,
)
```

Save as an importable `my_world.py`. Run `python -m agent_world --world my_world:WORLD`.
`--universe` selects an isolated world instance, not a Python module name.
Loading a module is trusted server configuration; Agents cannot install code with a tool argument.

## World API

`FunctionSpec` declares name, version, input schema, optional output schema, read/write mode,
handler, optional `authorize(ctx, arguments)` and optional `visible_to(ctx)`.
Authorization must return exactly `True`; hiding a tool does not authorize or deny its execution.
A hidden function needs an authorization predicate when guessing its name must also be denied.

`FunctionContext` exposes only universe-scoped operations in the portable API:

- `get_state`, `get_state_record`, `set_state`, `delete_state`, `list_state`;
- public `get_role`, universe-local `get_activity`;
- server `now`, actor identity and operation identity;
- `random_int`, with draws saved in a successful write receipt.

`set_state` and `delete_state` accept `expected_version`. Tombstones preserve version continuity.
The separate state journal retains before/after contents until explicit privileged pruning.
`list_state` uses bounded pages and scoped opaque cursors; pages across separate requests are not a frozen snapshot.
`StateRule` matches literal scope/key prefixes. Every matching rule applies.
Undeclared state writes fail with `strict_state=True`; an optional state policy can further restrict SDK reads/writes.
A role's profile is world-independent; rules such as health, inventory, tasks and relationships remain world state.

## Execution and retries

Read functions have an object argument schema, no operation ID, no operation receipt and no emitted events.
They run against a physically read-only database connection. Writes cannot invoke a read function as a mutation.
Handlers, policy hooks, initializers, bootstrap hooks and migrations are synchronous trusted Python.
No callback may commit, attach a database, change pragmas or mutate credential tables through its supplied connection.
Output/schema/event failure rolls back the complete write transaction.

A write uses an `operation_id` unique to one actor in one universe.
The same function, arguments and activity target replay the committed receipt; changing intent conflicts.
Changing a claim epoch for the same activity does not change the intended action.
An accepted retry does not draw new random numbers or repeat committed mutations.
`world.get_receipt` / `GET /v1/receipts/{operation_id}` recovers a result without rerunning rules,
even when the original function is no longer available.

Guarantees cover this SQLite transaction and retained receipts, NOT arbitrary external side effects.
Do not make network/payment/file-system writes inside handlers. A future external-effect integration needs its own delivery contract.
Cancellation or a lost response does not prove a write failed: query its receipt and retry only the same operation ID.
A missing receipt means no commit was visible at lookup, not proof that an in-flight request can never commit.

## Identity, activities and recovery

Bearer identity tokens bind a role and universe. Tokens and join tickets are stored as hashes, not plaintext.
A join ticket creates at most one credential, and can replay that credential during its validity window.
Ticket revocation prevents exchange/recovery; it does not revoke an identity already issued from it.
Token revoke/rotate are separate operator actions. Old tickets cannot resurrect a rotated/revoked credential.

Authenticated admission is rechecked inside the transaction. Expiry after queuing for a write lock is not ignored.
Reads see an authorized snapshot; revocation does not retroactively erase a response already produced.
MCP sessions are bound to the credential that initialized them; rotation requires a new session.
The installed MCP SDK's per-message HTTP request supplies identity, not a session-inherited ContextVar.

Activity exclusivity is per role and declared group, not a global shared-resource lock.
`world.claim_activity`, `world.renew_claim` and `world.finish_activity` implement finite leases,
stale-epoch fencing and explicit complete/cancel. Model shared scarce resources with transactional world rules.

`world.bootstrap` returns a bounded world-defined view and its `snapshot_cursor` from one transaction.
Use `world.discover` for compact, paged function discovery; request full schemas only when needed.
The default bootstrap does not repeat all tool schemas. `include_catalog` is an explicit compatibility option.
`world.get_changes` and `world.wait_changes` read recipient events with a bounded cursor page.
Wait has a 30-second ceiling, supports cancellation, and checks durable data across server processes without model polling.
Retention makes old cursors expire explicitly. Recover current state with bootstrap; old private event history is not reconstructed.
World authors must place enduring facts in state and implement a sufficient, authorized bootstrap view.

## Versioning and migration

`api_version` is the required SDK API, `version` is the world package contract,
`state_version` is the world's persisted data schema, and each function has its own version.
Bump world version for rules or policy changes, even if Python code changes are not detectable from a JSON manifest.
Bump function version when its descriptor changes. Changing read/write mode requires a new function ID.
Bump state version for data migration; provide every step in `migrations={target_version: callable}`.

Initialization runs once. Migration, state validation, registry changes and manifest changes commit together.
Failure leaves prior data and version intact. Missing steps, downgrades and same-version manifest drift are rejected.
After upgrade, an old process loaded with a previous world definition refuses new rule execution.
Restart it with the matching package. A committed receipt can still be recovered.
Back up real databases before upgrading; the additive runtime schema currently has version 2.
Existing values are journaled once as an explicit baseline; unavailable old versions are not reconstructed.

## Data and observation plane

Agent and browser-player intents use the same gateway, identity checks, rules and receipts.
Optional `ViewSpec` projections serve authorized snapshots and checkpoint-relative deltas.
State history, recipient notifications and derived view caches have separate purposes and retention.
See [WORLD_DATA.md](WORLD_DATA.md) for the contract, client usage and remaining boundaries.

## Deployment and explicit limits

Install with `pip install .`; web assets are included in the wheel. Imports do not create databases.
`python -m agent_world` serves web, HTTP and MCP on one origin and one database.
For a reverse proxy, configure an HTTPS `--public-url` with the externally visible hostname.
Do not disable MCP Host/Origin protections to make a proxy appear to work.
The old `module:app` entry points remain source-checkout shims; package entry points are preferred.
Serving defaults to authenticated access. Legacy in-process factories retain explicit test/compatibility paths.

The backend is a local file-backed SQLite database, with WAL and one concurrent writer.
This is not distributed storage, a high-availability deployment, or a malicious-code sandbox.
World code is trusted. The legacy `ctx.conn` escape hatch is not portable; use the SDK rather than raw SQL.
Callbacks can access Python/OS facilities, so do not load arbitrary user-supplied packages.

The web Basic Auth and operator key are for a trusted operator prototype, NOT independent end-user ownership.
Production accounts, credential recovery, durable token-rotation response recovery, rate limits,
backup automation, monitoring, large-scale fanout and hostile-plugin isolation are outside this revision.
Token rotation currently returns a replacement immediately; a lost replacement response requires operator intervention.
The two rule examples are adaptation/conformance examples, not a full D&D implementation or proof of an engaging product.
