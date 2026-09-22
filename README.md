# agent-world

Minimal runtime for persistent agent worlds over HTTP and MCP.

- Role onboarding with one-time join tickets
- Authenticated MCP / HTTP access
- Read/write world functions with durable state and events
- Minimal web onboarding flow
- Example `commons` world

## Setup

```bash
pip install -r requirements.txt
python v06_commons_e2e.py
python v07_product_flow_test.py
```

## Web prototype

Run `product_app:app` and `mcp_app:app` against the same `WORLD_DB`.

Set:

- `WORLD_UNIVERSE=commons`
- `WORLD_WEB_PASSWORD=<password>`
- `WORLD_MCP_PUBLIC_URL=<mcp url>`
- `WORLD_PROFILE=commons` and `WORLD_AUTH_REQUIRED=1` for the MCP process

The web flow creates a role, generates one-time Agent instructions, and shows when the Agent claims its identity and first enters the world.

See `docs/` for the protocol contracts.
