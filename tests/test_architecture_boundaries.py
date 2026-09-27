"""Static guards for the L5 dependency boundary.

These checks intentionally avoid naming external demo/test worlds: ad-hoc consumers
must not become architectural authorities.
"""

from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOP_LEVEL = {
    "fastapi", "starlette", "uvicorn", "httpx", "mcp",
    "tests", "examples", "experiments",
}
ADAPTERS_AND_CONSUMERS = {
    "application", "http_app", "mcp_app", "product_app", "onboarding_app",
    "transport_contracts", "universe_loader", "builtin_worlds", "web",
    "demo_universe", "commons_universe", "world_zero_universe",
}


def forbidden_imports(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = ("agent_world." if node.level else "") + (node.module or "")
            names = [prefix] + [prefix.rstrip(".") + "." + item.name for item in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] in FORBIDDEN_TOP_LEVEL or (
                parts[0] == "agent_world"
                and len(parts) > 1
                and parts[1] in ADAPTERS_AND_CONSUMERS
            ):
                found.append(name)
    return found


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_core_sdk_and_public_sdk_do_not_import_adapters_or_consumers(self):
        package = ROOT / "agent_world"
        paths = set(package.glob("runtime_*.py")) | set(package.glob("world_*.py"))
        paths |= {package / "presentation.py", package / "retention.py", package / "__init__.py"}
        paths.discard(package / "world_zero_universe.py")
        for path in sorted(paths):
            with self.subTest(module=path.name):
                self.assertEqual(forbidden_imports(path.read_text(encoding="utf-8")), [])

    def test_guard_detects_nested_relative_and_consumer_imports(self):
        self.assertTrue(forbidden_imports("def f():\n    import mcp\n"))
        self.assertTrue(forbidden_imports("from . import http_app\n"))
        self.assertTrue(forbidden_imports("from agent_world.product_app import create_product_app\n"))
        self.assertTrue(forbidden_imports("import examples.workflow_world\n"))
        self.assertTrue(forbidden_imports("import experiments.cross_world_identity\n"))
        self.assertEqual(forbidden_imports("from .world_context import FunctionContext\n"), [])


if __name__ == "__main__":
    unittest.main()
