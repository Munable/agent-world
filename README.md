# agent-world

A persistent world runtime for people and agents, with a portable Python rules SDK.

## Run

```sh
python -m pip install -e .
# Set WORLD_WEB_PASSWORD in your environment to enable the operator web prototype.
python -m agent_world --world world-zero --db world.sqlite3
```

Web, HTTP and MCP share one database and origin. Default: `http://127.0.0.1:8000`.
The web username defaults to `operator`; world calls require a Bearer identity token.

## Build a world

Export a `WorldDefinition` from an installed Python module, then run:

```sh
python -m agent_world --world my_world:WORLD --universe campaign --db campaign.sqlite3
```

See [the SDK contract](docs/FOUNDATION.md), [the audit](docs/FOUNDATION_AUDIT.md),
[world data and views](docs/WORLD_DATA.md), [durable time](docs/DURABLE_TIME.md), and the independent
[encounter](examples/encounter_world.py) and [workflow](examples/workflow_world.py) examples.

## Test

```sh
python tools/run_tests.py
python tools/check_package.py
node tests/view_client.test.mjs
```

Prototype operator access is not a multi-user account system. Run only trusted world code.
