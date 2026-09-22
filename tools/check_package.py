from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        wheels = base / "wheels"
        target = base / "installed"
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
                str(ROOT),
            ],
            check=True,
        )
        wheel = next(wheels.glob("agent_world-*.whl"))
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
                "assert 'installed' in str(Path(agent_world.__file__).resolve())",
                "assert not list(Path.cwd().glob('*.sqlite3'))",
                "assert files('agent_world').joinpath('web', 'index.html').is_file()",
                "assert files('agent_world').joinpath('web', 'world-client.js').is_file()",
                "from agent_world import ViewSpec",
                "w = WorldRuntime(Path.cwd()/'package-test.sqlite3')",
                "get_universe_installer('examples.workflow_world:WORLD')(w, 'custom')",
                "assert w.get_world_manifest('custom')['world_id'] == 'workflow-example'",
                "print('WHEEL_IMPORTS_ASSETS_EXTERNAL_WORLD_OK')",
            ]
        )
        subprocess.run([sys.executable, "-c", code], cwd=base, env=env, check=True)


if __name__ == "__main__":
    main()
