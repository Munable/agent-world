"""Repository guard for current critical contract coverage.

This does not prove behavior; the named tests still execute normally. It only makes
accidental deletion of current contract coverage visible.
"""

from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "test_world_sdk.py": {
        "test_internal_sql_cannot_bypass_managed_state_schema",
        "test_internal_sql_cannot_bypass_managed_state_authorizer",
        "test_state_authorizer_uses_authorization_state_api",
    },
    "test_managed_state_boundary.py": {
        "test_sdk_self_revocation_is_not_reauthorized_after_the_write",
        "test_migration_validates_final_schema_not_transient_shapes",
        "test_timer_raw_schema_failure_cannot_commit_state",
    },
}


class RegressionGuardTests(unittest.TestCase):
    def test_reproduced_regressions_remain_discoverable(self):
        for filename, required in EXPECTED.items():
            with self.subTest(module=filename):
                tree = ast.parse((ROOT / "tests" / filename).read_text(encoding="utf-8"))
                names = {
                    node.name
                    for cls in tree.body
                    if isinstance(cls, ast.ClassDef)
                    for node in cls.body
                    if isinstance(node, ast.FunctionDef)
                }
                self.assertFalse(
                    required - names,
                    f"critical regressions removed: {required - names}",
                )


if __name__ == "__main__":
    unittest.main()
