from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        wheels = base / "wheels"
        target = base / "installed"
        # Build from a tool-owned source snapshot, so setuptools intermediates
        # never overwrite pre-existing checkout metadata or another task's files.
        source = base / "source"
        source.mkdir()
        for name in ("pyproject.toml", "README.md", "requirements.txt", "LICENSE"):
            shutil.copy2(ROOT / name, source / name)
        for name in ("agent_world", "examples"):
            shutil.copytree(ROOT / name, source / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheels),
                str(source),
            ],
            check=True,
        )
        wheel = next(wheels.glob("agent_world-*.whl"))
        print(json.dumps({
            "wheel": wheel.name,
            "bytes": wheel.stat().st_size,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }, sort_keys=True), flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
            check=True,
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(target)
        code = "\n".join(
            [
                "from pathlib import Path",
                "import agent_world",
                "import json, time",
                "from importlib.resources import files",
                "from importlib.metadata import version",
                "from agent_world import WorldRuntime",
                "from agent_world.application import create_application",
                "from agent_world.universe_loader import get_universe_installer",
                "import agent_world.http_app, agent_world.mcp_app, agent_world.product_app, agent_world.onboarding_app",
                "import examples.river_camp, examples.river_camp_app, examples.camp_campaign",
                "import examples.dnd_world, examples.dnd_app, examples.dnd_handbook",
                "from starlette.testclient import TestClient",
                "assert 'installed' in str(Path(agent_world.__file__).resolve())",
                "assert agent_world.__version__ == version('agent-world')",
                "assert not list(Path.cwd().glob('*.sqlite3'))",
                "assert files('agent_world').joinpath('web', 'index.html').is_file()",
                "assert files('agent_world').joinpath('web', 'world-client.js').is_file()",
                "assert all(files('agent_world').joinpath('web', name).is_file() for name in ('commons.html', 'commons.css', 'commons.js', 'commons-client.js'))",
                "assert 'installed' in str(Path(examples.river_camp.__file__).resolve())",
                "assert 'installed' in str(Path(examples.river_camp_app.__file__).resolve())",
                "assert 'installed' in str(Path(examples.camp_campaign.__file__).resolve())",
                "assert examples.camp_campaign.WORLD.world_id == 'river-camp-campaign'",
                "assert all(files('examples').joinpath('river_camp_web', name).is_file() for name in ('index.html', 'camp.css', 'camp.js', 'camp-client.js', 'camp-presentation.js'))",
                "assert 'installed' in str(Path(examples.dnd_world.__file__).resolve())",
                "assert examples.dnd_world.WORLD.world_id == 'old-lighthouse-dnd'",
                "assert all(files('examples').joinpath('dnd_web', name).is_file() for name in ('index.html', 'styles.css', 'dnd.js', 'dnd-client.js'))",
                "import agent_world.connect",
                "from agent_world.connect import connect_world",
                "from agent_world import ViewSpec, TimerSpec",
                "import agent_world.timer_worker",
                "w = WorldRuntime(Path.cwd()/'package-test.sqlite3')",
                "get_universe_installer('examples.workflow_world:WORLD')(w, 'custom')",
                "assert w.get_world_manifest('custom')['world_id'] == 'workflow-example'",
                "get_universe_installer('examples.river_camp:WORLD')(w, 'camp')",
                "assert w.get_world_manifest('camp')['world_id'] == 'river-camp-example'",
                "get_universe_installer('examples.camp_campaign:WORLD')(w, 'campaign')",
                "assert w.get_world_manifest('campaign')['world_id'] == 'river-camp-campaign'",
                "for mode, port in (('river', 8770), ('campaign', 8772)):",
                "    origin = f'http://127.0.0.1:{port}'",
                "    camp_app = examples.river_camp_app.create_camp_application(Path.cwd()/f'{mode}-package.sqlite3', mode=mode)",
                "    with TestClient(camp_app, base_url=origin) as client:",
                "        for route, media in (('/', 'text/html'), ('/camp-static/camp.css', 'text/css'), ('/camp-static/camp.js', 'javascript'), ('/camp-static/camp-client.js', 'javascript'), ('/camp-static/camp-presentation.js', 'javascript'), ('/static/commons-client.js', 'javascript')):",
                "            response = client.get(route)",
                "            assert response.status_code == 200, (mode, route, response.status_code)",
                "            assert media in response.headers['content-type'], (mode, route, response.headers)",
                "            assert response.content, (mode, route)",
                "        assert '/camp-static/camp.js' in client.get('/').text",
                "        if mode == 'campaign':",
                "            assert client.get('/v1/bootstrap').status_code == 401",
                "            private = Path.cwd()/'campaign-private'",
                "            connection = connect_world(origin, key_path=private/'identity.json', output_path=private/'connect.json', new_identity=True, client=client)",
                "            assert time.time() < connection.expires_at <= time.time() + 86401",
                "            headers = json.loads(connection.output_path.read_text())['mcp']['agent_world']['headers']",
                "            bootstrap = client.get('/v1/bootstrap', headers=headers)",
                "            assert bootstrap.status_code == 200, bootstrap.status_code",
                "            entry = bootstrap.json()['world_entry_state']['view']",
                "            assert not entry['is_founder'] and not entry['is_judge']",
                "            joined = client.post('/v1/functions/camp.join/invoke', headers=headers, json={'operation_id': 'wheel-campaign-join', 'arguments': {}})",
                "            assert joined.status_code == 200, joined.status_code",
                "            inspected = client.post('/v1/functions/camp.inspect/invoke', headers=headers, json={'arguments': {}})",
                "            assert inspected.status_code == 200 and isinstance(inspected.json()['result']['campaign'], dict)",
                "            grant = client.post('/v1/functions/camp.set_judge/invoke', headers=headers, json={'operation_id': 'wheel-campaign-self-grant', 'arguments': {'role_id': connection.role_id, 'enabled': True, 'authority_epoch': 1}})",
                "            assert grant.status_code == 403, grant.status_code",
                "dnd_db = Path.cwd()/'dnd-data'/'world.sqlite3'",
                "setup = examples.dnd_app.initialize_local(dnd_db, Path.cwd()/'dnd-private')",
                "with TestClient(examples.dnd_app.create_dnd_application(dnd_db), base_url='http://127.0.0.1:8796') as client:",
                "    for route in ('/', '/dnd-static/styles.css', '/dnd-static/dnd.js', '/dnd-static/dnd-client.js', '/static/commons-client.js'):",
                "        response = client.get(route)",
                "        assert response.status_code == 200 and response.content, (route, response.status_code)",
                "    connected = connect_world('http://127.0.0.1:8796', key_path=Path(setup['participants']['A']['key_path']), output_path=Path.cwd()/'dnd-private'/'connect.json', client=client)",
                "    headers = json.loads(connected.output_path.read_text())['mcp']['agent_world']['headers']",
                "    joined = client.post('/v1/functions/dnd.join/invoke', headers=headers, json={'operation_id':'wheel-dnd-join','arguments':{'name':'Wheel explorer','class_id':'rogue'}})",
                "    assert joined.status_code == 200, joined.text",
                "    seen = client.post('/v1/functions/dnd.look/invoke', headers=headers, json={'arguments':{}})",
                "    assert seen.status_code == 200 and seen.json()['result']['scene']['id'] == 'camp'",
                "print('DND_WHEEL_IDENTITY_STATIC_ROUTES_GAMEPLAY_OK')",
                "print('WHEEL_IMPORTS_ASSETS_PACKAGE_PROBE_OK')",
                "print('RIVER_CAMP_WHEEL_IMPORTS_STATIC_ROUTES_OK')",
                "print('CAMPAIGN_WHEEL_DEFAULT_IDENTITY_PERMISSIONS_ASSETS_OK')",
            ]
        )
        subprocess.run([sys.executable, "-c", code], cwd=base, env=env, check=True)
        project = base / "independent-world"
        project.mkdir()
        for name in ("package_probe_world.py", "package_probe_acceptance.py"):
            shutil.copy2(ROOT / "tests" / "fixtures" / name, project / name)
        expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        (project / "pyproject.toml").write_text(
            '[project]\nname="agent-world-package-probe"\nversion="0.0.0"\ndependencies=["agent-world==' + expected + '"]\n',
            encoding="utf-8")
        subprocess.run([sys.executable, "package_probe_acceptance.py", expected], cwd=project, env=env, check=True)


if __name__ == "__main__":
    main()
