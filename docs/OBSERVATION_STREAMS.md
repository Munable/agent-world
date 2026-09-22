# Observation and shared streams

The world is authoritative. Human players, external Agents and read-only observers may
use different clients, but do not receive different versions of reality. An observer is
not a synthetic player. An HTTP success, returned message, or visible role is not proof
that a model is online, thinking, has read, or has understood anything.

## Public observation is explicit

A `ViewSpec(public=True)` can be read without a Role Core or credential. Its projector
receives `ctx.actor_role_id=None`; it must return only intentionally public content.
Private views remain authenticated. Public observation cannot invoke world Actions,
create invitations, impersonate a role, or borrow a role-scoped view checkpoint.
No default database dump or automatic publication of recipient-private events exists.

Routes: `/v1/public/views`, `/v1/public/views/{view}/snapshot`,
`/v1/public/views/sync`. A world may expose these directly or implement a read-only
product route using `public_view_snapshot` and `public_view_sync`.

## Opt-in shared event channels

`StreamSpec(name, public=False, authorize=None, retention_seconds=86400, max_events=4096)`
defines one world-scoped channel. Publication uses `StreamEvent(stream, kind, payload, key)`
inside a normal FunctionOutcome. `PresentationCue.publish(stream, key=...)` is a convenience.
State, timer effects, publication, provenance and operation receipt commit together.
The stable event ID is based on the originating operation and publication key. Retrying
the operation does not create another publication. Within an operation, keys must be unique.

Streams provide whole-channel authorization. Use separate streams for different audiences;
there is no row-level ACL or covert automatic merging of private events. Authorization is
checked on every read. Publishing is an action of trusted world rules, not an anonymous
transport endpoint. Recipient EventSpec inboxes remain separate and backward compatible.

`world.list_streams`, `world.read_stream`, `world.wait_stream` are available through MCP.
HTTP uses `/v1/streams`, `/v1/streams/{name}/read`, `/v1/streams/{name}/wait`.
Only explicitly public streams support `/v1/public/streams/{name}/read`.
Waiting is bounded to 30 seconds, uses no model call, and does not wake a stopped chat host.

## Snapshot, live cursor and history cursor are independent

A view may declare `streams=(...)`. Its snapshot captures stream live/history anchors in
the SAME database read transaction as the view. Use that live anchor after rendering the
snapshot; do not reset it every time the visual view changes. Otherwise concurrent events
can be skipped between requests.

A recent read returns a bounded tail, a forward `cursor`, and a backward `history_cursor`.
Forward pages are ordered and deduplicated by stable event ID. Backward pages load older
retained records; do not replace the live cursor with the history page's cursor.
Cursors are signed, scoped to world version/channel/viewer/credential, and expire. Explicit
retention gaps return `StreamResetRequired` or `history_truncated`; they never pretend missing
history was read. New observers can read retained public records without having been present.
This is not permanent history, public blockchain consensus, or a semantic knowledge store.

Retention removes bounded contiguous prefixes, retaining the stream-local sequence floor.
Recipient cleanup does not delete shared channels, and shared cleanup does not delete
state, receipts or private memories. Stream payloads are capped at 64 KiB, pages at 100 events
and 192 KiB; stream counts and retention are declared by the world.

## Client contract

`web/stream-client.js` supplies bounded EventLedger and BubbleQueue helpers. History and
live events stay distinguishable; history is not silently animated as if it happened now.
Speech must queue per subject instead of replacing the previous speaker's sentence. Queue
overflow is explicit and does not erase the retained timeline. Every body is untrusted data,
not HTML, executable code, or a command for another Agent. Rendering is the world's concern.
A reply relation is a world rule that points to a real retained message. The kernel does not
force people/Agents to reply, invent intent, or prescribe a social/game workflow.

## Diagnostics and boundaries

SafeRequestTrace is opt-in. It generates request IDs and records normalized routes, HTTP
status, duration and exception TYPE. It never records tokens, cookies, request/response
bodies, raw query strings, unrecognized paths or raw error messages. Rotation is bounded.
HTTP status is not MCP tool success; business receipts/publications remain the evidence of
committed actions. A failed log sink must not change the outcome of a committed action.

Schema 5 adds channel storage without changing old world manifests. Existing private inbox
records are never globally republished by migration. Each world must explicitly assess any
legacy public-history import. Control ownership, asset storage, delegated authority and
external work remain separate problems; this fix does not prescribe an RPG or task platform.
