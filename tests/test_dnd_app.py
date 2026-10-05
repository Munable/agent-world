"""Public identity/HTTP entry and private provisioning for the playable example."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from starlette.testclient import TestClient

from agent_world.connect import connect_world
from examples.dnd_app import create_dnd_application, existing_world, initialize_local


class DndApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dnd-entry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / "data" / "world.sqlite3"
        self.private = self.root / "keys"
        self.setup = initialize_local(self.db, self.private)
        self.origin = "http://127.0.0.1:8796"

    def connect(self, client, label="A", *, access="control", suffix="one"):
        connection = connect_world(self.origin, key_path=Path(self.setup["participants"][label]["key_path"]),
                                   output_path=self.private / f"{label}-{suffix}.json",
                                   access_mode=access, client=client)
        return connection, json.loads(connection.output_path.read_text())["mcp"]["agent_world"]["headers"]

    def call(self, client, headers, name, args, operation=None):
        body = {"arguments": args}
        if operation:
            body["operation_id"] = operation
        return client.post("/v1/functions/" + name + "/invoke", headers=headers, json=body)

    def test_packaged_entry_assets_and_anonymous_auth_boundary(self):
        with TestClient(create_dnd_application(self.db), base_url=self.origin) as client:
            for route in ("/", "/dnd-static/styles.css", "/dnd-static/dnd.js",
                          "/dnd-static/dnd-client.js", "/static/commons-client.js"):
                response = client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertTrue(response.content)
            self.assertEqual(client.get("/v1/bootstrap").status_code, 401)
            self.assertEqual(client.post("/v1/functions/dnd.look/invoke", json={"arguments": {}}).status_code, 401)

    def test_key_recovers_character_across_application_restart_and_new_credential(self):
        with TestClient(create_dnd_application(self.db), base_url=self.origin) as client:
            connection, headers = self.connect(client)
            response = self.call(client, headers, "dnd.join", {"name": "Same owner", "class_id": "rogue"}, "join-once")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.call(client, headers, "dnd.act", {"action": "move", "target": "entry"}, "move-once").status_code, 200)
            before = self.call(client, headers, "dnd.look", {}).json()["result"]
        with TestClient(create_dnd_application(self.db), base_url=self.origin) as client:
            resumed, headers = self.connect(client, suffix="two")
            self.assertEqual(connection.role_id, resumed.role_id)
            again = self.call(client, headers, "dnd.join", {"name": "Must not reset", "class_id": "fighter"}, "re-enter")
            self.assertEqual(again.status_code, 200, again.text)
            after = self.call(client, headers, "dnd.look", {}).json()["result"]
            self.assertEqual(after["character"], before["character"])
            self.assertEqual(after["scene"]["id"], "entry")
            replay = self.call(client, headers, "dnd.act", {"action": "move", "target": "entry"}, "move-once")
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.json()["replayed"])

    def test_observe_and_ordinary_player_cannot_gain_referee_authority(self):
        with TestClient(create_dnd_application(self.db), base_url=self.origin) as client:
            _, control = self.connect(client)
            self.assertEqual(self.call(client, control, "dnd.join", {"name": "Player", "class_id": "fighter"}, "join").status_code, 200)
            _, observe = self.connect(client, access="observe", suffix="observe")
            self.assertEqual(self.call(client, observe, "dnd.look", {}).status_code, 200)
            self.assertEqual(self.call(client, observe, "dnd.say", {"text": "write through observe"}, "denied-write").status_code, 403)
            self.assertEqual(self.call(client, control, "dnd.handbook", {"chapter": "gm"}).status_code, 403)
            self.assertFalse(self.call(client, control, "dnd.look", {}).json()["result"]["authority"]["can_rule"])
            _, referee = self.connect(client, label="GM")
            self.assertEqual(self.call(client, referee, "dnd.handbook", {"chapter": "gm"}).status_code, 200)
            self.assertTrue(self.call(client, referee, "dnd.look", {}).json()["result"]["authority"]["can_rule"])

    def test_init_and_wrong_world_never_reset_existing_world_or_identity(self):
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        before = {p: p.read_bytes() for p in self.private.rglob("*.json")}
        with self.assertRaises(ValueError):
            initialize_local(self.db, self.private)
        self.assertEqual({p: p.read_bytes() for p in self.private.rglob("*.json")}, before)
        with self.assertRaises(ValueError):
            existing_world(self.db, "wrong-universe")
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(self.db)
        with self.assertRaises(ValueError):
            existing_world(alias, "old-lighthouse")
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
