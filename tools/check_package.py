from __future__ import annotations

from pathlib import Path
import os
import shutil
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
                "from agent_world import ViewSpec, TimerSpec",
                "import agent_world.timer_worker",
                "w = WorldRuntime(Path.cwd()/'package-test.sqlite3')",
                "get_universe_installer('examples.workflow_world:WORLD')(w, 'custom')",
                "assert w.get_world_manifest('custom')['world_id'] == 'workflow-example'",
                "print('WHEEL_IMPORTS_ASSETS_EXTERNAL_WORLD_OK')",
            ]
        )
        subprocess.run([sys.executable, "-c", code], cwd=base, env=env, check=True)
        project = base / "independent-world"
        project.mkdir()
        for name in ("reference_world.py", "reference_acceptance.py"):
            shutil.copy2(ROOT / "tests" / "fixtures" / name, project / name)
        import tomllib
        expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        (project / "pyproject.toml").write_text(
            '[project]\nname="reference-world-probe"\nversion="0.0.0"\ndependencies=["agent-world==' + expected + '"]\n',
            encoding="utf-8")
        subprocess.run([sys.executable, "reference_acceptance.py", expected], cwd=project, env=env, check=True)


if __name__ == "__main__":
    main()
