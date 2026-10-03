"""Isolated provisioning, static entry and portable SDK connection regressions."""
from __future__ import annotations

from contextlib import closing
import asyncio
import json
import io
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from agent_world import WorldRuntime
from agent_world.connect import connect_world
from examples.river_camp_app import create_camp_application, initialize_local, main
from tests.live_server import LiveServer


@unittest.skipUnless(os.name == "posix", "Local provisioning requires POSIX private-file permissions")
class RiverCampAppTests(unittest.TestCase):
    def setUp(self):
        original_umask = os.umask(0o077)
        os.umask(original_umask)
        self.addCleanup(os.umask, original_umask)
        self.temp = tempfile.TemporaryDirectory(prefix="river-camp-app-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "database" / "camp.sqlite3"
        self.private = self.root / "private"

    def provision(self):
        return initialize_local(self.db, self.private, "local-camp")

    def test_private_provisioning_reuses_persistent_authority_after_default_restart(self):
        setup = self.provision()
        for directory in (self.db.parent, self.private, *(self.private / name for name in setup["participants"])):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (self.db, self.private / "setup.json", *(Path(p["key_path"]) for p in setup["participants"].values())):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = WorldRuntime(self.db).get_state("local-camp", "camp:world", "authority")
        for index in range(2):
            with TestClient(create_camp_application(self.db, universe="local-camp", port=8765),
                            base_url="http://127.0.0.1:8765") as client:
                connection = connect_world("http://127.0.0.1:8765",
                                           key_path=Path(setup["participants"]["GM"]["key_path"]),
                                           output_path=self.private / f"gm-connect-{index}.json", client=client)
                self.assertEqual(connection.role_id, setup["participants"]["GM"]["role_id"])
                auth = json.loads(connection.output_path.read_text())["mcp"]["agent_world"]["headers"]
                boot = client.get("/v1/bootstrap", headers=auth)
                self.assertEqual(boot.status_code, 200, boot.text)
                self.assertTrue(boot.json()["world_entry_state"]["view"]["is_judge"])
        self.assertEqual(WorldRuntime(self.db).get_state("local-camp", "camp:world", "authority"), before)

    def test_plain_default_application_never_grants_first_visitor_judge(self):
        with TestClient(create_camp_application(self.db, universe="local-camp", port=8765),
                        base_url="http://127.0.0.1:8765") as client:
            connected = connect_world("http://127.0.0.1:8765", key_path=self.private / "ordinary.json",
                                      output_path=self.private / "ordinary-connect.json", new_identity=True, client=client)
            auth = json.loads(connected.output_path.read_text())["mcp"]["agent_world"]["headers"]
            join = client.post("/v1/functions/camp.join/invoke", headers=auth,
                               json={"arguments": {}, "operation_id": "ordinary-join"})
            self.assertEqual(join.status_code, 200, join.text)
            boot = client.get("/v1/bootstrap", headers=auth).json()
            self.assertFalse(boot["world_entry_state"]["view"]["is_judge"])
            self.assertFalse(boot["world_entry_state"]["view"]["is_founder"])
            grant = client.post("/v1/functions/camp.set_judge/invoke", headers=auth,
                                json={"operation_id": "self-grant", "arguments": {
                                    "role_id": connected.role_id, "enabled": True, "authority_epoch": 1}})
            self.assertEqual(grant.status_code, 403, grant.text)

    def test_repeated_initialization_refuses_without_changing_database_or_keys(self):
        self.provision()
        files = [self.db, *self.private.rglob("*.json")]
        original = {path: path.read_bytes() for path in files}
        with self.assertRaises(ValueError):
            self.provision()
        self.assertEqual({path: path.read_bytes() for path in files}, original)

    def test_preexisting_private_data_and_symbolic_setup_fail_before_database_creation(self):
        self.private.mkdir(mode=0o700)
        (self.private / "Founder").mkdir(mode=0o700)
        (self.private / "Founder" / "keep.txt").write_text("existing personal data")
        with self.assertRaises(ValueError):
            self.provision()
        self.assertFalse(self.db.exists())
        self.assertEqual((self.private / "Founder" / "keep.txt").read_text(), "existing personal data")
        other_db = self.root / "other-database" / "camp.sqlite3"
        other_private = self.root / "other-private"
        other_private.mkdir(mode=0o700)
        (other_private / "setup.json").symlink_to(self.root / "missing-target")
        with self.assertRaises(ValueError):
            initialize_local(other_db, other_private, "local-camp")
        self.assertFalse(other_db.exists())

    def test_shared_or_repository_secret_directory_is_rejected_without_chmod_or_partial_database(self):
        self.private.mkdir(mode=0o755)
        os.chmod(self.private, 0o755)
        with self.assertRaises(ValueError):
            self.provision()
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o755)
        self.assertFalse(self.db.exists())
        repo = self.root / "synthetic-repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        with self.assertRaises(ValueError):
            initialize_local(self.root / "separate" / "db.sqlite3", repo / "private", "local-camp")
        self.assertFalse((self.root / "separate" / "db.sqlite3").exists())
        self.assertFalse((repo / "private").exists())

    def test_cli_reuse_rejects_wrong_universe_and_symlink_without_starting_server(self):
        self.provision()
        original = self.db.read_bytes()
        alias = self.root / "alias.sqlite3"
        alias.symlink_to(self.db)
        for db, universe in ((self.db, "mistyped-camp"), (alias, "local-camp")):
            with self.subTest(db=db, universe=universe), patch("sys.argv", ["river-camp", "--db", str(db), "--universe", universe]), patch("sys.stderr", new=io.StringIO()), patch("uvicorn.run") as run, self.assertRaises(SystemExit):
                main()
            run.assert_not_called()
        self.assertEqual(self.db.read_bytes(), original)

    def test_application_reuse_rejects_wrong_universe_before_runtime_construction(self):
        self.provision()
        original = self.db.read_bytes()
        with patch("examples.river_camp_app.create_application") as create, self.assertRaises(ValueError):
            create_camp_application(self.db, universe="mistyped-camp")
        create.assert_not_called()
        self.assertEqual(self.db.read_bytes(), original)

    def test_cross_profile_reuse_refuses_before_loading_package_or_changing_local_data(self):
        self.provision()
        # A same-universe mismatch must fail even before the other module loads.
        for stored_id, mode in (("river-camp-example", "campaign"), ("river-camp-campaign", "river")):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                manifest = json.loads(conn.execute("SELECT manifest_json FROM world_definitions WHERE universe=?",
                                                  ("local-camp",)).fetchone()[0])
                manifest["world_id"] = stored_id
                conn.execute("UPDATE world_definitions SET manifest_json=? WHERE universe=?",
                             (json.dumps(manifest), "local-camp"))
            # Read-only SQLite may create WAL coordination sidecars. Compare
            # authoritative data and identity files, not transient read marks.
            paths = [self.db, *(path for path in self.private.rglob("*") if path.is_file())]
            original_files = set(path for path in self.root.rglob("*") if path.is_file())
            original = {path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode))
                        for path in paths}
            with self.subTest(stored_id=stored_id, mode=mode), patch("examples.river_camp_app.create_application") as create, \
                    patch("examples.river_camp_app.get_world_definition") as load, self.assertRaises(ValueError):
                create_camp_application(self.db, universe="local-camp", mode=mode)
            create.assert_not_called()
            load.assert_not_called()
            cli = ["river-camp", "--db", str(self.db), "--universe", "local-camp"]
            if mode == "campaign":
                cli.append("--campaign")
            with patch("sys.argv", cli), patch("sys.stderr", new=io.StringIO()), \
                    patch("uvicorn.run") as run, self.assertRaises(SystemExit):
                main()
            run.assert_not_called()
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode))
                              for path in paths}, original)
            added = set(path for path in self.root.rglob("*") if path.is_file()) - original_files
            self.assertLessEqual(added, {Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")})

    def test_unknown_mode_refuses_before_provisioning_or_runtime_construction(self):
        with patch("examples.river_camp_app.create_application") as create:
            with self.assertRaises(ValueError):
                create_camp_application(self.db, mode="other")
            with self.assertRaises(ValueError):
                initialize_local(self.db, self.private, mode="other")
        create.assert_not_called()
        self.assertFalse(self.db.parent.exists())
        self.assertFalse(self.private.exists())

    def test_selected_package_wrong_world_id_refuses_before_creating_local_data(self):
        wrong = SimpleNamespace(world_id="river-camp-example")
        with patch("examples.river_camp_app.get_world_definition", return_value=wrong), \
                patch("examples.river_camp_app.create_application") as create:
            with self.assertRaises(ValueError):
                initialize_local(self.db, self.private, mode="campaign")
            with self.assertRaises(ValueError):
                create_camp_application(self.db, mode="campaign")
        create.assert_not_called()
        self.assertFalse(self.db.parent.exists())
        self.assertFalse(self.private.exists())

    def test_campaign_provisioning_reuses_formal_identity_and_persistent_authority(self):
        setup = initialize_local(self.db, self.private, mode="campaign")
        self.assertEqual(setup["universe"], "river-camp-campaign")
        self.assertEqual(setup["world_profile"], "examples.camp_campaign:WORLD")
        self.assertEqual(setup["world_id"], "river-camp-campaign")
        before = WorldRuntime(self.db).get_state(setup["universe"], "camp:world", "authority")
        for index in range(2):
            with TestClient(create_camp_application(self.db, mode="campaign"),
                            base_url="http://127.0.0.1:8772") as client:
                connected = connect_world("http://127.0.0.1:8772",
                                          key_path=Path(setup["participants"]["GM"]["key_path"]),
                                          output_path=self.private / f"campaign-gm-{index}.json", client=client)
                self.assertEqual(connected.role_id, setup["participants"]["GM"]["role_id"])
                self.assertGreater(connected.expires_at, time.time())
                self.assertLessEqual(connected.expires_at, time.time() + 86401)
                auth = json.loads(connected.output_path.read_text())["mcp"]["agent_world"]["headers"]
                boot = client.get("/v1/bootstrap", headers=auth)
                self.assertEqual(boot.status_code, 200, boot.text)
                self.assertTrue(boot.json()["world_entry_state"]["view"]["is_judge"])
                joined = client.post("/v1/functions/camp.join/invoke", headers=auth,
                                     json={"arguments": {}, "operation_id": "campaign-gm-join"})
                self.assertEqual(joined.status_code, 200, joined.text)
                inspected = client.post("/v1/functions/camp.inspect/invoke", headers=auth,
                                        json={"arguments": {}})
                self.assertEqual(inspected.status_code, 200, inspected.text)
                self.assertIn("campaign", inspected.json()["result"])
        self.assertEqual(WorldRuntime(self.db).get_state(setup["universe"], "camp:world", "authority"), before)

    def test_campaign_default_application_never_grants_first_visitor_judge(self):
        with TestClient(create_camp_application(self.db, mode="campaign"),
                        base_url="http://127.0.0.1:8772") as client:
            connected = connect_world("http://127.0.0.1:8772", key_path=self.private / "ordinary.json",
                                      output_path=self.private / "ordinary-connect.json", new_identity=True, client=client)
            auth = json.loads(connected.output_path.read_text())["mcp"]["agent_world"]["headers"]
            join = client.post("/v1/functions/camp.join/invoke", headers=auth,
                               json={"arguments": {}, "operation_id": "ordinary-campaign-join"})
            self.assertEqual(join.status_code, 200, join.text)
            boot = client.get("/v1/bootstrap", headers=auth).json()
            self.assertFalse(boot["world_entry_state"]["view"]["is_judge"])
            self.assertFalse(boot["world_entry_state"]["view"]["is_founder"])
            grant = client.post("/v1/functions/camp.set_judge/invoke", headers=auth,
                                json={"operation_id": "campaign-self-grant", "arguments": {
                                    "role_id": connected.role_id, "enabled": True, "authority_epoch": 1}})
            self.assertEqual(grant.status_code, 403, grant.text)

    def test_cli_selects_independent_defaults_and_preserves_explicit_options(self):
        for campaign, options, universe, port in (
            (False, [], "river-camp", 8770), (True, [], "river-camp-campaign", 8772),
            (True, ["--universe", "custom", "--port", "8999"], "custom", 8999),
        ):
            args = ["river-camp", "--db", str(self.db), *options]
            if campaign:
                args.append("--campaign")
            with self.subTest(campaign=campaign, options=options), patch("sys.argv", args), \
                    patch("examples.river_camp_app._existing_camp", return_value=self.db) as existing, \
                    patch("examples.river_camp_app.create_camp_application") as create, patch("uvicorn.run") as run:
                main()
            mode = "campaign" if campaign else "river"
            existing.assert_called_once_with(self.db, universe, mode=mode)
            create.assert_called_once_with(self.db, universe=universe, port=port, mode=mode)
            run.assert_called_once_with(create.return_value, host="127.0.0.1", port=port)

    def test_cli_reuse_rejects_shared_storage_without_repairing_permissions(self):
        self.provision()
        for path, wide_mode, private_mode in ((self.db, 0o644, 0o600), (self.db.parent, 0o755, 0o700)):
            os.chmod(path, wide_mode)
            with patch("sys.argv", ["river-camp", "--db", str(self.db), "--universe", "local-camp"]), patch("sys.stderr", new=io.StringIO()), patch("uvicorn.run") as run, self.assertRaises(SystemExit):
                main()
            run.assert_not_called()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), wide_mode)
            os.chmod(path, private_mode)

    def test_wrapper_serves_all_local_assets_and_existing_identity_endpoints(self):
        self.provision()
        with TestClient(create_camp_application(self.db, universe="local-camp")) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn('/camp-static/camp.js', home.text)
            self.assertEqual(home.headers["cache-control"], "no-store")
            for asset in ("/camp-static/camp.js", "/camp-static/camp.css", "/static/commons-client.js", "/static/world-client.js"):
                response = client.get(asset)
                self.assertEqual(response.status_code, 200, asset)
                self.assertGreater(len(response.content), 100)
            # The generic operator UI stays disabled; public key entry below
            # and the existing portable client do not depend on that endpoint.
            self.assertEqual(client.get("/api/config").status_code, 503)
            self.assertEqual(client.get("/v1/bootstrap").status_code, 401)
            self.assertEqual(client.get("/camp-static/../private/setup.json").status_code, 404)


class _CampServer(LiveServer):
    """Reuse the owned-port/lifecycle harness while launching the example CLI."""
    def __init__(self, *, mode="river"):
        super().__init__()
        self.mode = mode

    def start(self):
        self.db = self.db.resolve()
        if not self.db.exists():
            self.setup = initialize_local(self.db, Path(self.temp.name).resolve() / "private", "network", mode=self.mode)
        real_popen = subprocess.Popen

        def launch(_args, **kwargs):
            command = [sys.executable, "-u", "-m", "examples.river_camp_app", "--db", str(self.db),
                       "--universe", "network", "--port", str(self.port)]
            if self.mode == "campaign":
                command.append("--campaign")
            return real_popen(command, **kwargs)

        with patch("tests.live_server.subprocess.Popen", side_effect=launch):
            super().start()


@unittest.skipUnless(os.name == "posix", "Local provisioning requires POSIX private-file permissions")
class RiverCampAppNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_key_connection_restarts_and_reads_pending_intent_over_real_mcp(self):
        await self._assert_key_connection_recovers("river")

    async def test_campaign_key_connection_restarts_and_reads_campaign_over_real_mcp(self):
        await self._assert_key_connection_recovers("campaign")

    async def _assert_key_connection_recovers(self, mode):
        async with _CampServer(mode=mode) as server:
            key_path = Path(server.setup["participants"]["A"]["key_path"])
            output = key_path.parent / "opencode.json"
            connection = await asyncio.to_thread(connect_world, server.url, key_path=key_path, output_path=output)
            token = json.loads(output.read_text())["mcp"]["agent_world"]["headers"]["Authorization"].removeprefix("Bearer ")
            async with server.session({"token": token}) as (session, _):
                joined = await session.call_tool("camp.join", arguments={"arguments": {}, "operation_id": "sdk-join"})
                self.assertFalse(joined.is_error, joined.structured_content)
                proposal = await session.call_tool("camp.propose", arguments={"operation_id": "sdk-propose", "arguments": {
                    "intent_id": "sdk-pending", "text": "Inspect the old anchors before deciding the crossing"}})
                self.assertFalse(proposal.is_error, proposal.structured_content)
            await asyncio.to_thread(server.restart)
            recovered = await asyncio.to_thread(connect_world, server.url, key_path=key_path,
                                                output_path=output, replace_config=True)
            self.assertEqual(recovered.role_id, connection.role_id)
            self.assertEqual(recovered.public_key, connection.public_key)
            token = json.loads(output.read_text())["mcp"]["agent_world"]["headers"]["Authorization"].removeprefix("Bearer ")
            async with server.session({"token": token}) as (session, _):
                state = await session.call_tool("camp.inspect", arguments={"arguments": {}})
                self.assertFalse(state.is_error, state.structured_content)
                if mode == "campaign":
                    self.assertIn("campaign", state.structured_content["result"])
                intent = next(i for i in state.structured_content["result"]["intents"] if i["key"] == "sdk-pending")
                self.assertEqual(intent["value"]["owner"], recovered.role_id)
                self.assertEqual(intent["value"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
