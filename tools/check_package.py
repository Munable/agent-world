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
                "from importlib.resources import files",
                "from agent_world import WorldRuntime",
                "from agent_world.application import create_application",
                "from agent_world.universe_loader import get_universe_installer",
                "import agent_world.http_app, agent_world.mcp_app, agent_world.product_app, agent_world.onboarding_app",
                "import examples.river_camp, examples.river_camp_app",
                "from starlette.testclient import TestClient",
                "assert 'installed' in str(Path(agent_world.__file__).resolve())",
                "assert not list(Path.cwd().glob('*.sqlite3'))",
                "assert files('agent_world').joinpath('web', 'index.html').is_file()",
                "assert files('agent_world').joinpath('web', 'world-client.js').is_file()",
                "assert all(files('agent_world').joinpath('web', name).is_file() for name in ('commons.html', 'commons.css', 'commons.js', 'commons-client.js'))",
                "assert 'installed' in str(Path(examples.river_camp.__file__).resolve())",
                "assert 'installed' in str(Path(examples.river_camp_app.__file__).resolve())",
                "assert all(files('examples').joinpath('river_camp_web', name).is_file() for name in ('index.html', 'camp.css', 'camp.js', 'camp-client.js'))",
                "import agent_world.connect",
                "from agent_world import ViewSpec, TimerSpec",
                "import agent_world.timer_worker",
                "w = WorldRuntime(Path.cwd()/'package-test.sqlite3')",
                "get_universe_installer('examples.workflow_world:WORLD')(w, 'custom')",
                "assert w.get_world_manifest('custom')['world_id'] == 'workflow-example'",
                "get_universe_installer('examples.river_camp:WORLD')(w, 'camp')",
                "assert w.get_world_manifest('camp')['world_id'] == 'river-camp-example'",
                "camp_app = examples.river_camp_app.create_camp_application(Path.cwd()/'camp-package.sqlite3')",
                "with TestClient(camp_app) as client:",
                "    for route, media in (('/', 'text/html'), ('/camp-static/camp.css', 'text/css'), ('/camp-static/camp.js', 'javascript'), ('/camp-static/camp-client.js', 'javascript'), ('/static/commons-client.js', 'javascript')):",
                "        response = client.get(route)",
                "        assert response.status_code == 200, (route, response.status_code)",
                "        assert media in response.headers['content-type'], (route, response.headers)",
                "        assert response.content, route",
                "    assert '/camp-static/camp.js' in client.get('/').text",
                "print('WHEEL_IMPORTS_ASSETS_PACKAGE_PROBE_OK')",
                "print('RIVER_CAMP_WHEEL_IMPORTS_STATIC_ROUTES_OK')",
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
