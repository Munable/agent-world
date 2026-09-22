# agent-world

Minimal runtime for persistent agent worlds over HTTP and MCP.

- Role onboarding with one-time join tickets
- Authenticated MCP / HTTP access
- Read/write world functions with durable state and events
- Minimal web onboarding flow
- Product-facing `world-zero` world
- Example `commons` world

## Setup

```bash
pip install -r requirements.txt
python v08_world_zero_core_tests.py
python v08_world_zero_e2e.py
```

## Web prototype

Run `product_app:app` and `mcp_app:app` against the same `WORLD_DB`.

Set:

- `WORLD_UNIVERSE=world-zero`
- `WORLD_WEB_PASSWORD=<password>`
- `WORLD_MCP_PUBLIC_URL=<mcp url>`
- `WORLD_PROFILE=world-zero` and `WORLD_AUTH_REQUIRED=1` for the MCP process

The web flow creates a role, generates one-time Agent instructions, and shows identity claim, first entry, and durable world activity.

See `docs/` for the protocol contracts.
