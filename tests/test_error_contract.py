from __future__ import annotations

import math
import unittest

from agent_world.errors import (
    AuthenticationRequired,
    InvalidArguments,
    RuleViolation,
    StorageBusy,
)
from agent_world.transport_contracts import error_response


class ErrorContractTests(unittest.TestCase):
    def test_world_error_metadata_is_structured_and_bounded(self):
        error = RuleViolation(
            "later",
            retryable=True,
            recovery="retry_after_condition",
            retry_after_seconds=2.5,
            details={"scope": "probe", "remaining": 0},
        )
        status, payload = error_response(error)
        self.assertEqual(status, 409)
        self.assertEqual(
            payload,
            {
                "error": "RuleViolation",
                "message": "later",
                "retryable": True,
                "recovery": "retry_after_condition",
                "retry_after_seconds": 2.5,
                "details": {
                    "scope": "probe",
                    "remaining": 0,
                },
            },
        )

    def test_invalid_public_error_metadata_is_rejected(self):
        with self.assertRaises(TypeError):
            RuleViolation("bad", retryable="yes")
        with self.assertRaises(ValueError):
            RuleViolation("bad", recovery="")
        with self.assertRaises(ValueError):
            RuleViolation(
                "bad",
                retry_after_seconds=-1,
            )
        with self.assertRaises(ValueError):
            RuleViolation(
                "bad",
                retry_after_seconds=math.inf,
            )
        with self.assertRaises(TypeError):
            RuleViolation(
                "bad",
                details={1: "not-a-string-key"},
            )
        with self.assertRaises(TypeError):
            RuleViolation(
                "bad",
                details={"unsafe": object()},
            )

    def test_builtin_recovery_hints_do_not_require_message_parsing(self):
        self.assertEqual(
            error_response(
                AuthenticationRequired("missing")
            )[1]["recovery"],
            "authenticate",
        )
        self.assertEqual(
            error_response(
                InvalidArguments("bad")
            )[1]["recovery"],
            "fix_request",
        )
        status, payload = error_response(
            StorageBusy("locked")
        )
        self.assertEqual(status, 503)
        self.assertTrue(payload["retryable"])
        self.assertEqual(
            payload["recovery"],
            "retry_same_operation",
        )


if __name__ == "__main__":
    unittest.main()
