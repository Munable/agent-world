"""A non-Commons consumer must be connectable through the portable identity helper."""
from pathlib import Path
import json
import os
import tempfile
import time
import unittest

from starlette.testclient import TestClient

from agent_world.application import create_application
from agent_world.connect import connect_world


@unittest.skipUnless(os.name == "posix", "The connection helper requires POSIX private-file permissions")
class ApplicationIdentityTests(unittest.TestCase):
    def test_external_world_connection_is_bounded_and_same_key_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = create_application(
                root / "world.sqlite3", world_profile="examples.river_camp:WORLD",
                universe="camp-connection", public_base_url="http://127.0.0.1:8770",
            )
            with TestClient(app, base_url="http://127.0.0.1:8770") as client:
                first = connect_world(
                    "http://127.0.0.1:8770", key_path=root / "private" / "key.json",
                    output_path=root / "private" / "first.json", new_identity=True, client=client,
                )
                self.assertGreater(first.expires_at, time.time())
                self.assertLessEqual(first.expires_at, time.time() + 86401)
                config = json.loads(first.output_path.read_text())
                auth = config["mcp"]["agent_world"]["headers"]["Authorization"]
                joined = client.post("/v1/functions/camp.join/invoke", headers={"Authorization": auth},
                                     json={"arguments": {}, "operation_id": "camp-connect-join"})
                self.assertEqual(joined.status_code, 200)
                second = connect_world(
                    "http://127.0.0.1:8770", key_path=first.key_path,
                    output_path=root / "private" / "second.json", client=client,
                )
                self.assertEqual(first.role_id, second.role_id)
                self.assertEqual(first.public_key, second.public_key)
                next_auth = json.loads(second.output_path.read_text())["mcp"]["agent_world"]["headers"]["Authorization"]
                state = client.post("/v1/functions/camp.inspect/invoke", headers={"Authorization": next_auth},
                                    json={"arguments": {}})
                self.assertEqual(state.status_code, 200)
                self.assertEqual(state.json()["result"]["role_id"], first.role_id)


if __name__ == "__main__":
    unittest.main()
