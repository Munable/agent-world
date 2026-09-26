"""Small repository guards; not a semantic proof or a general architecture framework."""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = {"fastapi", "starlette", "uvicorn", "httpx", "mcp", "tests", "examples",
            "lantern_hollow", "ashen_vault"}
ADAPTERS = {"application", "http_app", "mcp_app", "product_app", "onboarding_app",
            "transport_contracts", "universe_loader", "builtin_worlds", "web",
            "demo_universe", "commons_universe", "world_zero_universe"}


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
            if parts[0] in EXTERNAL or (parts[0] == "agent_world" and len(parts) > 1
                                       and parts[1] in ADAPTERS):
                found.append(name)
    return found


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_core_and_sdk_do_not_import_adapters_or_worlds(self):
        package = ROOT / "agent_world"
        paths = set(package.glob("runtime_*.py")) | set(package.glob("world_*.py"))
        paths |= {package / "presentation.py", package / "retention.py"}
        paths.discard(package / "world_zero_universe.py")
        for path in sorted(paths):
            with self.subTest(module=path.name):
                self.assertEqual(forbidden_imports(path.read_text(encoding="utf-8")), [])

    def test_guard_detects_nested_and_relative_imports(self):
        self.assertTrue(forbidden_imports("def f():\n    import mcp\n"))
        self.assertTrue(forbidden_imports("from . import http_app\n"))
        self.assertTrue(forbidden_imports("from agent_world.product_app import create_product_app\n"))
        self.assertTrue(forbidden_imports("import examples.workflow_world\n"))
        self.assertEqual(forbidden_imports("from .world_context import FunctionContext\n"), [])

    def test_reproduced_regressions_remain_discoverable(self):
        expected = {
            "test_world_sdk.py": {
                "test_legacy_raw_sql_cannot_bypass_managed_state_schema",
                "test_legacy_raw_sql_cannot_bypass_managed_state_authorizer",
                "test_state_authorizer_can_inspect_shared_state_without_legacy_connection",
            },
            "test_managed_state_boundary.py": {
                "test_sdk_self_revocation_is_not_reauthorized_after_the_write",
                "test_migration_validates_final_schema_not_transient_shapes",
                "test_timer_raw_schema_failure_cannot_commit_state",
            },
        }
        for filename, required in expected.items():
            with self.subTest(module=filename):
                tree = ast.parse((ROOT / "tests" / filename).read_text(encoding="utf-8"))
                names = {node.name for cls in tree.body if isinstance(cls, ast.ClassDef)
                         for node in cls.body if isinstance(node, ast.FunctionDef)}
                self.assertFalse(required - names, f"critical regressions removed: {required - names}")


if __name__ == "__main__":
    unittest.main()
