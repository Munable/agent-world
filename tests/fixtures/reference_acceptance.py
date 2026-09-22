"""Runs outside the checkout, against the installed wheel and one external world module."""
from pathlib import Path
from importlib.metadata import version
import json
import sys
import time
from unittest.mock import patch

import agent_world
from agent_world import WorldRuntime
from agent_world.world_sdk import install_world
from agent_world.application import create_application
from fastapi.testclient import TestClient
from reference_world import WORLD

BASE = Path.cwd().resolve()
EXPECTED = sys.argv[1]
assert version("agent-world") == EXPECTED
assert "installed" in str(Path(agent_world.__file__).resolve())
assert not (BASE / "agent_world").exists(), "the external project must not contain a copied kernel"

DB = BASE / "reference.sqlite3"
runtime = WorldRuntime(DB)
install_world(runtime, "reference", WORLD)
role = runtime.create_role("Visitor")
identity = runtime.issue_identity_token("reference", role["role_id"])
observer = runtime.issue_identity_token("reference", role["role_id"], access_mode="observe")
auth = {"Authorization": "Bearer " + identity["token"]}
view_auth = {"Authorization": "Bearer " + observer["token"]}
app = create_application(DB, world_profile="reference_world:WORLD", universe="reference", timers_enabled=False)
with TestClient(app) as client:
    snapshot = client.post("/v1/views/scene/snapshot", headers=view_auth, json={})
    assert snapshot.status_code == 200, snapshot.text
    cursor = snapshot.json()["timeline_cursor"]
    request = {"operation_id": "one", "arguments": {"move_id": "one", "destination": 4}}
    assert client.post("/v1/functions/character.move/invoke", headers=view_auth, json=request).status_code == 403
    accepted = client.post("/v1/functions/character.move/invoke", headers=auth, json=request)
    assert accepted.status_code == 200, accepted.text
    replay = client.post("/v1/functions/character.move/invoke", headers=auth, json=request)
    assert replay.json()["replayed"] is True
    assert replay.json()["commit_seq"] == accepted.json()["commit_seq"]
    # A fresh host instance recovers the timer using the installed external module.
    restarted = WorldRuntime(DB)
    install_world(restarted, "reference", WORLD)
    with patch("time.time", return_value=time.time() + 10):
        assert restarted.run_due_timers("reference")["count"] == 1
    timeline = client.post("/v1/views/timeline", headers=view_auth, json={"cursor": cursor})
    assert timeline.status_code == 200, timeline.text
    assert [e["cue"]["phase"] for e in timeline.json()["events"]] == ["start", "finish"]
    current = client.post("/v1/views/scene/snapshot", headers=view_auth, json={}).json()
    assert current["snapshot"]["entities"][role["role_id"]]["position"] == 4
    for index, channel in enumerate(("intent", "speech")):
        result = client.post("/v1/functions/character.express/invoke", headers=auth, json={
            "operation_id": "expression-" + str(index), "arguments": {
                "message_id": "bubble-" + str(index), "channel": channel, "text": "Hello from a real declared expression."}})
        assert result.status_code == 200, result.text
    bubbles = client.post("/v1/views/timeline", headers=view_auth, json={"cursor": current["timeline_cursor"]}).json()
    assert [e["cue"]["channel"] for e in bubbles["events"]] == ["intent", "speech"]
    with patch("time.time", return_value=time.time() + 100000):
        removed = restarted.apply_retention("reference")
    assert removed["configured"]
    with restarted._conn(readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events WHERE universe='reference'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM state_changes WHERE universe='reference'").fetchone()[0] == 0
    gap = client.post("/v1/views/timeline", headers=view_auth, json={"cursor": current["timeline_cursor"]})
    assert gap.status_code == 409 and gap.json()["recovery"] == "reset_view"
    receipt = client.get("/v1/receipts/one", headers=auth)
    assert receipt.status_code == 200
    assert client.post("/v1/functions/character.move/invoke", headers=auth, json=request).json()["replayed"] is True
    runtime.revoke_identity_token(observer["token_id"])
    assert client.post("/v1/views/scene/snapshot", headers=view_auth, json={}).status_code == 401
print(json.dumps({"external_world": "passed", "copied_kernel": False, "package_version": EXPECTED,
                  "control_and_observe": True, "timer_restart": True, "ordered_cues": True,
                  "public_bubbles": True, "retention_gap_recovery": True}))
