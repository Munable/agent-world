# Durable world time 0.11

Timers are server-owned world obligations, not sleeping Agent conversations or stored
bearer credentials. A human UI and an Agent request the same world rules; those rules
may register a one-shot timer in the same transaction as their state change.

## Declare a timer

```python
from agent_world import TimerSpec, FunctionOutcome

def expire(ctx, arguments):
    ctx.set_state("effects", arguments["id"], {"active": False})
    return FunctionOutcome({"expired": True})

# WorldDefinition(..., timers=(TimerSpec("expire", expire, input_schema),))
# In an authorized write rule:
# ctx.schedule_timer("effect-123", "expire", {"id": "123"}, due_at=ctx.now + 60)
```

`TimerSpec` defines a synchronous handler, object input schema, optional output schema,
optional `authorize(ctx, arguments)`, version, retry budget and base retry delay.
Timer handlers are NOT FunctionSpec tools and cannot be invoked by guessing their names
through HTTP/MCP. They are loaded only from the trusted world package.

`ctx.schedule_timer(id, handler, arguments, due_at=...)` buffers a validated command.
`ctx.cancel_timer(id)` buffers cancellation. Both require a managed world write;
read functions and view projections cannot use them. Initializers and migrations can
schedule timers too. Unknown handlers or invalid commands roll back the entire operation.
`ctx.get_timer(id)` reads bounded metadata for world rules, not a public raw queue.
A world function must authorize its own cancellation and visibility policies.

## Authority

Scheduling is authorized by the initiating world Action. Once committed, the timer is a
world obligation. It does not borrow the creator's token, and revoking that token or taking
the creator offline does not automatically revoke the world's obligation.
The handler runs as `system:timer`, with a frozen `ctx.timer` record containing the ID,
due time, original initiator/operation and attempt. Existing state policies still apply.
`TimerSpec.authorize` rechecks world preconditions at firing, under the database write lock.
A world can explicitly reject settlement after an owner is disabled, or continue a game
cooldown regardless. Neither behavior is silently universalized by the runtime.
This is NOT a general "act as this user later" credential/delegation system.

## Atomicity, cancellation and recovery

Scheduling, state changes, commit provenance and the originating receipt commit together.
The timer is not visible before commit. Rule/output failure removes all of them.
A timer's ID is unique within its universe. An identical schedule is a no-op even after
completion/cancellation; changing its intent conflicts. Use a new ID for a new occurrence.

Each firing runs in one short transaction. State/history/notifications, result receipt,
follow-up timer commands and the terminal timer status commit together. Multiple workers
serialize at the SQLite writer lock; a completed item cannot fire again.
Crashing before commit rolls the attempt back to pending. Its callback may run again after
restart, but only one committed set of database effects is accepted. No exactly-once claim
is made for external network/filesystem/payment side effects; they remain forbidden here.

Cancellation wins only when its transaction commits before firing. Cancelling a completed
timer does not undo effects. Cancelling a cancelled timer is idempotent. Cancelling an
unknown timer fails. A timer cannot cancel itself while firing.

Timers pin world ID/version and handler version. Upgrading rules does not silently reinterpret
queued work: old timers become `blocked`. An old loaded process refuses to drive a newer world.
A migration can explicitly cancel old IDs and schedule replacements. No automatic rebind.

## Failure and catch-up

- `RetryTimer` explicitly requests rollback and retry, with capped exponential delay.
- At most the configured attempts (1..10) are committed as attempts; exhaustion is `failed`.
- A denied precondition or rule rejection becomes `rejected` without world mutations.
- Unexpected handler errors become `failed`; arbitrary exception messages/arguments are not exposed.
- Storage failures roll back the transaction and leave work pending for a later sweep.

A failed/rejected/blocked item does not stop other due items. Per-sweep candidates are fixed
and bounded, so an immediate follow-up or retry cannot create an unbounded same-sweep loop.
One transaction may change at most 32 timers; one universe may hold 10,000 pending timers.
Terminal records remain to prevent ID reuse; lifecycle transitions link to commit provenance.
This revision does not add an automatic terminal-record purge or permanent archival service.

`due_at` is a finite UTC Unix timestamp, not a wall-clock string or a per-world game tick.
The worker rechecks the clock after acquiring the write lock. Clock rollback can delay work;
a wrong/forward-jumping host clock can still affect due decisions. Synchronize the host clock.
Overdue one-shot timers fire when service resumes; missed recurring periods are not fabricated.
`ctx.now` is execution time; `ctx.timer.due_at` is the requested deadline. Rules decide how to
handle lateness. This is not a hard real-time guarantee or a universal fairness/turn model.

## Running

The combined `python -m agent_world` server drives timers during its lifespan, even with
no clients connected. `--no-timers` disables that embedded worker. `--timer-interval` controls
polling (default one second). A deployment can instead run a separate trusted worker:

```sh
python -m agent_world.timer_worker --world my_world:WORLD --universe campaign --db world.sqlite3
python -m agent_world.timer_worker --world my_world:WORLD --universe campaign --db world.sqlite3 --once
```

The standalone worker and the combined server must use the same world package and database.
Only the combined server starts a worker by default; legacy standalone HTTP/MCP app factories
need an external worker. Shutdown waits for an in-flight short rule to finish. Trusted rules
must not hang or do model/network work inside the transaction. No background service is
installed by importing the package or by running the conformance tests.

`examples/timed_worlds.py` supplies an expiring RPG effect and a workflow deadline using the
same contract. The automated tests exercise actual HTTP/MCP, process death mid-transaction,
concurrent workers, permission rechecks, retries, cancellation, migration and restart.
They are deterministic conformance tests, not claims about autonomous model behavior.

## Still separate

External-work delivery, per-user control/delegation, asset/blob retention, public/group
subscriptions and semantic event timelines remain independent foundation work. Timers do
not implement those systems, wake a remote chat application, or guarantee continuous hosting.
