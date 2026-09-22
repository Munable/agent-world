# Reference foundation gate RF1 (0.12)

Purpose: support an independent, small, complete reference world without copying the kernel.
This gate is not a promise that all possible worlds or public multi-user hosting are complete.

## Fixed boundary

| Layer | Owns | Must not own |
| --- | --- | --- |
| Kernel | role admission, atomic rules, receipts, recovery, observer synchronization, time, bounded data policies | game genre, model brain, private chain of thought |
| World package | rules, objects, visibility, movement semantics, public expression, active action state | another fork of Runtime |
| Client | input, renderer, interpolation, animation assets, UI feedback | authoritative outcomes or invented Agent thinking |
| Operator/product | accounts, credential issuance/recovery, hosting and backups | bypasses around world rules |

A browser player and an external Agent use the same authenticated world functions.
`control` credentials may invoke authorized actions. `observe` credentials only read the
same role-visible information; they do not grant public-spectator access to another role.
Issuance remains a trusted operator/library operation. Rotation preserves the access mode.
This is not an account, ownership-transfer, or general delegation system.

## Acceptance matrix

The following must pass before a reference project is treated as a separate consumer:

- Control: observer writes, activity changes, and write replays are rejected by Runtime,
  not just hidden in the UI. Revocation is enforced on established connections.
- State: failed writes leave no partial state/history; repeated intents never duplicate effects.
- Lifetime: the world explicitly chooses bounded notification/history retention; current
  state and idempotency receipts survive cleanup. Temporary historical values may be omitted.
- Observation: authorized view snapshots and deltas recover across fresh sessions and restart.
- Presentation: a start and finish between snapshots are both available in sequence while
  retained. Expired history requires a fresh snapshot, not a fabricated replay.
- Time: accepted timers continue without a live client and retain transaction/permission checks.
- Portability: install the built wheel, copy only an external world module into another directory,
  and run its control, observation, timers, presentation and retention without kernel source copies.

`tests/test_reference_boundary.py`, `tests/test_reference_transport.py`,
`tests/view_client.test.mjs`, and `tools/check_package.py` exercise this gate.
The packaging check creates an independent project with an exact package-version dependency.
It uses a locally built wheel; no claim of publishing this version to PyPI is made.

## Timeline correction

0.9 established a portable rule SDK, not a complete world platform.
0.10 added state history and net observer deltas; it did not preserve every intermediate action.
0.11 added world-owned timers; it did not wake remote Agent apps or implement an external job system.
0.12 closes this reference-consumer gate: observer-only credentials, explicit retention,
and ordered public presentation cues. Earlier unqualified "foundation finished" statements
are superseded by this bounded gate.

## What stays outside this gate

External program/payment/message delivery, generalized organizations/delegation, blob hosting,
full asset provenance, universal group feeds, distributed storage and public account systems
are not required to start a controlled reference world. They remain explicit future requirements
when a real world needs them. Do not mechanically add them before building a reference consumer.
The gate also does not certify complete gameplay, finished pixel art, accessibility or browser UX.

## Next deliverable

Create an independent reference repository, pin a reviewed kernel commit or locally built wheel,
and use a separate database/configuration. Do not vendor or fork the kernel source.
Build one visually finished interactive scene before extending its world size. It must include
real navigation, facing/idle/walk/interaction states, cancellation, dialogue/public-intent bubbles,
refresh/reconnect behavior and a playable beginning-to-end loop. World assets and animation frames
belong to that repository. Use original or properly licensed assets, not copied game assets.

When a shared contract defect appears, fix it in agent-world and update the pinned dependency.
When a gameplay/art defect appears, fix only the reference world. "Small and complete" is the
product target; more maps or more universal modules are not substitutes for acceptance.
