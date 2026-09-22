# World data and observer contract 0.10

## Boundary

A world may be an RPG played from a browser, an Agent community, a collaboration space,
or something else. Controllers propose intents; server rules decide their effects.
No human/Agent distinction grants extra authority. Both use the same role-bound HTTP/MCP gateway.
The operator web prototype remains separate from an end-user account system.

```
HTTP / MCP intent -> admission + world rules -> one transaction
  current state + state history + commit provenance + receipt + optional notifications

current state -> authorized ViewSpec -> snapshot / checkpoint-relative delta
                 browser, Agent, or another observer client
```

## Three different records

- `world_state`: current authoritative values and tombstone versions.
- `state_changes` + `world_commits`: before/after state records, grouped with actor,
  function, operation ID and world version for managed Actions and migrations.
- `events`: independently retained recipient notifications, NOT all state changes.

SQLite triggers capture state writes even when a rule emits no event. Failed transactions
leave none of their state, history, commit or receipt behind. Replays return the original
commit reference. Schema migration and its records commit together.
Direct privileged SQL writes are recorded but have no invented actor/commit provenance.
Changing state identity in-place is rejected; use delete/create. Portable SDK operations
remain universe-scoped, and managed Actions reject cross-universe SQL writes as well.

History is private server data, not an Agent-accessible feed. `read_state_history` and
`prune_state_history` are trusted operator/library operations; no raw-history HTTP/MCP
endpoint is exposed. Values may be sensitive. Deployments must choose backup, archival,
erasure and retention policies; this revision does not promise eternal storage.
Event cleanup does not remove state history. History pruning does not remove current state.
Old database values are imported as `baseline`; earlier versions cannot be fabricated.
A commit permits at most 512 state changes / 2 MiB of combined before/after contents.
This journal covers `world_state`, not every credential, activity or external file change.

## Declare an observer view

```python
from agent_world import ViewSpec

def project(ctx, arguments):
    value = ctx.get_state("world", "door", {"open": False})
    return {"entities": {"door": {"kind": "door", **value}}, "meta": {}}

# Add to WorldDefinition(..., views=(ViewSpec("scene", project),))
```

A ViewSpec declares a stable name, input schema, version, synchronous projection,
optional authorization/visibility callbacks, renderer hint and optional output schema.
Callbacks run under read-only guards. Authorization is checked on every snapshot/sync.
The world decides what each viewer can see. Do not return hidden fields and expect CSS
or a renderer to hide them. Read visibility changes must be reflected in the projection.
World/policy changes require a world version bump as with rule changes.

A projection returns `entities`, `resources`, and `meta` maps. Entity IDs are stable
within the view; entity contents are world-defined JSON. No mandatory health, coordinates,
tasks, currency, organization or social system. Relations can be ordinary typed entities.
A renderer hint is a label, never a URL for executable code.
Resource references may contain `uri`, `media_type`, `sha256`, `size`, `version`.
Only credential-free HTTPS or `asset://` references are accepted. The runtime neither
fetches these URIs nor turns them into HTML. These are references, NOT a blob store,
permanent asset registry, ownership/licensing system or proof that a remote file exists.

Each projection is bounded to 256 entities, 64 resources and 96 KiB. Larger worlds define
selector arguments (area, page, room, project, etc.). No whole-world dump is required.

## Snapshot and sync

- `GET /v1/views` / `world.list_views`: discover projections; schemas are opt-in.
- `POST /v1/views/{view}/snapshot` / `world.view_snapshot`: selector arguments -> snapshot.
- `POST /v1/views/sync` / `world.view_sync`: opaque checkpoint -> upserts/removals + new checkpoint.

Snapshot values and the internal state-history watermark are read in one database transaction.
The public `view_revision` hashes only the visible projected body; it does not expose global
journal offsets or hidden write counts. The watermark is retained privately in the checkpoint.
Sync recomputes the authorized projection, including changes to visibility or role metadata,
then compares it with the previous projected view. It never streams raw history.
Object/resource removal must be applied by the client, including when visibility is withdrawn.
Net deltas are NOT an animation timeline, individual event replay, or deterministic re-execution.
A delta can skip intermediate states; gameplay timelines need a separate world event contract.

Checkpoints bind universe, role, credential, view, selector and world/view versions.
They survive process restart, expire after five minutes, and are bounded to 16 per viewer
per universe / 1024 total. These are disposable derived caches, not permanent assets.
Expired/evicted/mismatched checkpoints return `ViewResetRequired`; clear/reload a snapshot.
401/403 means clear the protected view. Revocation cannot erase information already seen.
Apply a delta only when `base_cursor` matches the displayed cursor. Ignore out-of-order
replies; never apply a delta to another viewer or view. Resources are removed the same way.

`agent_world/web/world-client.js` provides `WorldClient` and `applyViewUpdate`.
It keeps credentials in caller-managed memory, rejects out-of-order updates, and uses
ordinary authenticated HTTP Actions with caller-supplied stable operation IDs.
It does not execute returned text or fetch resource URLs. UI code must render data as data.
Uncertain writes are not automatically repeated: query the receipt first.

## Limits and next foundation work

This release adds no genre-specific rule system. Encounter and workflow examples only
verify that different worlds use the same data/observation contracts.
Persistent world timers are covered by [DURABLE_TIME.md](DURABLE_TIME.md).
Remaining independent work includes durable external-effect delivery,
group/delegated authority, permanent asset/blob versioning and provenance, public/group
subscriptions, end-user identity/control ownership, archival policies, and large-view
materialization. None is replaced by this view cache or state journal.
SQLite and trusted synchronous world modules remain the execution boundary.
