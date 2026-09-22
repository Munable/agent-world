# Retention and presentation 0.12

## Control and observation

`issue_identity_token(..., access_mode="observe")` is a trusted issuer operation.
An observation credential reads as the specified role, subject to existing world/view policies.
It cannot invoke writes (including replay), start/claim/renew/finish activities or record a role
entry through bootstrap. Existing credentials migrate as `control`; rotation never upgrades
an observer. This does not provide anonymous spectators, user registration or delegation.
Do not put an operator credential into frontend JavaScript.

## Storage classes

`StateRule(..., history="metadata")` retains versions and change identity but omits before/after
values in newly committed history rows. `full` remains the compatibility default. When multiple
matching rules apply, any metadata rule suppresses historical values. Current state is unchanged.
This is not a secret-erasure guarantee: outcomes, notifications, old history, WAL pages, snapshots
and backups may independently contain data. Avoid putting private model memory in the server.
Rules should update logical actions, not write renderer frames to the database.

A world can declare:

```python
RetentionPolicy(event_seconds=3600, event_rows=1000,
                history_seconds=86400, history_rows=5000)
```

The combined application runs a bounded maintenance sweep every 30 seconds only when configured,
independently of the timer worker. `python -m agent_world.maintenance --world module:WORLD
--universe example --db world.sqlite3` runs one sweep for other deployments.
No policy means no new automatic destructive cleanup of existing worlds.

Cleanup removes only contiguous prefixes and advances the corresponding retention floor in
one transaction. It is universe-scoped and bounded per sweep. Bursts can temporarily exceed
row limits; monitor/call more sweeps if needed. Clock rollback does not create silent gaps.
Current state, commit metadata, timer IDs and operation receipts are not deleted. This protects
idempotency but means total database size is NOT bounded by these two policies. Receipt/asset
archival and full backup/erasure procedures are separate production decisions.
This is logical deletion, not secure erasure or guaranteed immediate file-size reduction.
SQLite reference: https://www.sqlite.org/pragma.html#pragma_secure_delete

## Public presentation cues

`PresentationCue(cue_id, subject_id, channel, phase, name, data).event(recipient_role_id)` creates
an ordinary EventSpec with a validated `world.presentation` envelope. No second delivery queue.

- channels: `action`, `speech`, `intent` (publicly declared intent, not private reasoning);
- phases: `start`, `finish`, `cancel`;
- `cue_id` correlates a lifecycle; `subject_id` identifies its visual world object;
- `name` and `data` are world-defined (move, speak, use an object, etc.);
- event time and actor attribution come from the server, not the frontend.

The engine validates envelope/size and commits cues with state and receipt, including timer
outcomes. The WORLD must authorize the subject, audience and lifecycle and keep any currently
active action in its state/view. This envelope alone does not enforce a movement or dialogue
state machine. Do not label an unobserved model process as "thinking". Visible intent is a
statement a world rule accepted. Text remains untrusted data, never executable markup.

A `ViewSpec(..., timeline=True)` makes public cues readable for that view. A snapshot returns
an opaque `timeline_cursor`, anchored to the same read transaction. The browser can independently
sync net state and drain ordered cues with `world.view_timeline` or `POST /v1/views/timeline`.
The latter takes `cursor` and an optional `limit`, and returns ordered `events`, a new cursor,
`base_cursor`, and `has_more`. Current view authorization is rechecked; only original recipient
cues whose subject is currently visible in that view are returned. World-specific audiences
are still decided by the rules, not inferred by the renderer.

Cursor binding includes role, credential, universe, view/selector and world version through the
existing bounded checkpoint cache. Expiry, eviction or a retention gap returns `ViewResetRequired`.
The client must reset to current authoritative state, cancel stale animation/bubble state and
NOT invent missing actions. New clients do not need every past animation to reconstruct a scene.
`WorldClient.readTimeline` drops stale concurrent replies, handles reset, and keeps the timeline
cursor separate from net-view synchronization. No global private sequence numbers are exposed
through this presentation endpoint. Ordinary recipient event APIs keep their existing contract.

No art, walking frames, pathfinding algorithm, camera or rendering engine is forced into the kernel.
Those belong to the consuming world/client. These are logical action cues, not 60 FPS frame streams.
