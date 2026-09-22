from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LEGACY = [
    "v02_core_tests.py",
    "auth_tests.py",
    "v05_onboarding_core_tests.py",
    "v05_onboarding_api_tests.py",
    "v06_read_function_tests.py",
    "v06_read_http_test.py",
    "mcp_auth_integration.py",
    "v05_full_entry_e2e.py",
    "v06_commons_e2e.py",
    "v07_product_flow_test.py",
    "v08_world_zero_core_tests.py",
    "v08_world_zero_e2e.py",
]


def run(command, cwd, env):
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout + result.stderr


def main():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    print("New foundation suite", flush=True)
    output = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], ROOT, env)
    print("\n".join(output.splitlines()[-4:]), flush=True)
    results = {}
    for port in (8776, 8825, 8826, 8835, 8836, 8845, 8846, 8855, 8856):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(
                    f"legacy test port {port} is occupied; no test request will be sent"
                ) from exc
    # Historical scripts create their own local fixture databases and logs.
    # Run them in a disposable source copy, never erase files in a developer checkout.
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp)
        for path in ROOT.glob("*.py"):
            shutil.copy2(path, target / path.name)
        for name in ("agent_world", "examples"):
            shutil.copytree(
                ROOT / name,
                target / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.sqlite3*", "*.log"),
            )
        env["PYTHONPATH"] = str(target)
        for script in LEGACY:
            print(script, flush=True)
            run([sys.executable, script], target, env)
            results[script] = "passed"
    print(json.dumps({"foundation": "passed", "legacy_suites": results}, indent=2))


if __name__ == "__main__":
    main()
