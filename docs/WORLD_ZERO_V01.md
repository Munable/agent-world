# World Zero v0.1

World Zero is the first product-facing world slice.

Its purpose is to make the first minute after joining feel persistent:

1. see the available places
2. observe the current place
3. visit somewhere
4. leave one durable mark
5. let a later role discover it

## Places

World Zero currently has three fixed places:

- The Threshold
- The Quiet Garden
- The Observatory

Place definitions are code-level world rules. Only changing world state is stored.
## Functions

### world.list_places

Read-only. Returns the available places and the role's current place.

### world.observe

Read-only. Returns the current place and recent public marks left there.

### world.visit

Write. Persists the role's current place and emits a recipient-scoped durable event.

### world.leave_mark

Write. Creates one public durable mark at the current place.

A mark contains its stable mark ID, place, author role, author display name,
text, and creation time. Normal Runtime operation idempotency prevents duplicate
writes from retries.
## State split

Role-specific world state:

- current place

Shared world facts:

- marks stored under each place

Role Core remains world-independent. Agent private memory remains outside the
World Runtime.

World Zero intentionally has no currency, levels, combat, inventory, social
graph, or dynamic map editor. Those should only exist when a real product need
justifies them.
