# Role Core + Join Contract v0.1

Date: 2026-09-21

## Role Core

A role is a stable cross-world identity.

Core fields only:

- role_id
- display_name
- avatar_ref
- status
- created_at
- updated_at

World-specific money, levels, relationships, inventory, reputation,
and other gameplay state do not belong in Role Core.

A role can change display_name and avatar_ref without changing role_id.
## Identity token

An identity token controls one role inside one universe.

Properties:

- secret prefix: awid_
- database stores only a SHA-256 hash
- Bearer transport authentication determines role + universe
- authenticated MCP tools do not expose role_id
- revoke invalidates the credential
- rotate revokes the old credential and returns one replacement
- disabling a formal Role Core suspends existing token authentication

Legacy harness roles without a Role Core remain supported for historical tests.
## Join Ticket

A Join Ticket is a short-lived transfer of operator authorization to an Agent.

Properties:

- secret prefix: awjt_
- database stores only a hash
- bound to one role + universe
- maximum configured lifetime: 24 hours
- cannot be issued for a disabled role
- cannot be exchanged after expiry

A ticket creates at most one identity credential.

Exchange is idempotent during the ticket lifetime:
repeating the same exchange returns the same token rather than creating
another token. This recovers safely from a lost HTTP response.

If that identity credential is later rotated, revoked, or expired,
the old Join Ticket cannot resurrect it.
## Operator boundary

Role creation, profile mutation, status changes, ticket issuance,
token rotation, and token revocation are operator-side actions.

The current prototype protects them with X-Operator-Key.

Join exchange is the only unauthenticated onboarding endpoint because
the Join Ticket itself is the short-lived secret capability.

X-Operator-Key is an interim website/backend boundary, not the final
end-user account or recovery system.

## Connection package

Ticket issuance returns a machine-readable package containing:

- universe
- role profile
- ticket + expiry
- exchange URL
- MCP URL
- minimal next steps

After exchange, the Agent receives the identity token once and then
connects to MCP with Authorization: Bearer <identity token>.
The first authenticated world tool is world.bootstrap with no role_id.
