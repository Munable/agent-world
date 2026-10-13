"""Runtime receipt evidence comes from the persisted original intent."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_world.runtime_core import WorldRuntime, FunctionOutcome
from agent_world.errors import OperationConflict


ARGS = {"type": "object", "properties": {
    "label": {"type": "string"}, "amount": {"type": "integer"}},
    "required": ["label", "amount"], "additionalProperties": False}


class RuntimeIntentReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / "work" / "tmp"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="intent-receipt-", dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.runtime = WorldRuntime(Path(self.temporary.name) / "world.sqlite3")
        self.addCleanup(self.runtime.close)
        self.role = self.runtime.create_role("Reader")["role_id"]
        self.calls = 0

        def handler(ctx, arguments):
            self.calls += 1
            arguments["amount"] = 999  # Handler mutation is not the caller's intent.
            return FunctionOutcome({"accepted": True})

        self.runtime.register_function("u", "test.write", 1, "Intent evidence", ARGS, handler)

    @staticmethod
    def proof(arguments):
        canonical = json.dumps({"function_id": "test.write", "arguments": arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"algorithm": "sha256-runtime-canonical-json-v1",
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}

    def invoke(self, arguments):
        return self.runtime.invoke_function("u", "test.write", self.role, deepcopy(arguments), operation_id="original")

    def test_new_receipt_proves_original_unicode_arguments_before_handler_mutation(self):
        arguments = {"label": "原始选择", "amount": 2}
        receipt = self.invoke(arguments)
        self.assertEqual(receipt["intent_fingerprint"], self.proof(arguments))
        self.assertEqual(self.runtime.get_receipt("u", self.role, "original")["intent_fingerprint"], self.proof(arguments))
        self.assertEqual(self.calls, 1)

    def test_old_saved_receipt_get_and_replay_add_proof_without_rewriting_stored_body(self):
        arguments = {"label": "旧记录", "amount": 2}
        receipt = self.invoke(arguments)
        receipt.pop("intent_fingerprint")
        legacy_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.runtime._conn() as connection:
            connection.execute("UPDATE operations SET receipt_json=? WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                               (legacy_json, "u", self.role, "original"))
        found = self.runtime.get_receipt("u", self.role, "original")
        replayed = self.invoke(arguments)
        self.assertEqual(found["intent_fingerprint"], self.proof(arguments))
        self.assertEqual(replayed["intent_fingerprint"], self.proof(arguments))
        self.assertTrue(replayed["replayed"])
        self.assertEqual(self.calls, 1)
        with self.runtime._conn(readonly=True) as connection:
            stored = connection.execute("SELECT receipt_json FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                                        ("u", self.role, "original")).fetchone()[0]
        self.assertEqual(stored, legacy_json)

    def test_conflicting_operation_keeps_only_original_commit_and_fingerprint(self):
        arguments = {"label": "same", "amount": 1}
        receipt = self.invoke(arguments)
        with self.assertRaises(OperationConflict):
            self.invoke({"label": "same", "amount": 2})
        found = self.runtime.get_receipt("u", self.role, "original")
        self.assertEqual(found["commit_seq"], receipt["commit_seq"])
        self.assertEqual(found["intent_fingerprint"], self.proof(arguments))
        self.assertEqual(self.calls, 1)
