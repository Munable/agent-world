from __future__ import annotations

import math
import sqlite3
import unittest

from agent_world.errors import (
    AuthenticationRequired,
    CursorExpired,
    InvalidArguments,
    ReceiptNotFound,
    RuleViolation,
    StorageBusy,
    TimerConflict,
    TimerNotFound,
    ViewNotFound,
    ViewResetRequired,
)
from agent_world.transport_contracts import error_response
from agent_world.world_streams import StreamResetRequired


class ErrorContractTests(unittest.TestCase):
    def test_special_error_routes_preserve_validated_metadata(self):
        for error_type, status, recovery in (
            (StreamResetRequired, 409, "read_stream_recent"),
            (ViewResetRequired, 409, "reset_view"),
            (ViewNotFound, 404, "retry_after_condition"),
            (TimerConflict, 409, "retry_after_condition"),
            (TimerNotFound, 404, "retry_after_condition"),
            (StorageBusy, 503, "retry_after_condition"),
        ):
            with self.subTest(error=error_type.__name__):
                # Exercise overrides of both the default False and StorageBusy's True.
                retryable = error_type is not StorageBusy
                error = error_type(
                    "public failure context",
                    retryable=retryable,
                    recovery="retry_after_condition",
                    retry_after_seconds=1.25,
                    details={"scope": "probe", "remaining": 0, "context": [None, True]},
                )
                self.assertEqual(
                    error_response(error),
                    (status, {
                        "error": error_type.__name__,
                        "message": "public failure context",
                        "retryable": retryable,
                        "recovery": recovery,
                        "retry_after_seconds": 1.25,
                        "details": {"scope": "probe", "remaining": 0, "context": [None, True]},
                    }),
                )

    def test_required_recovery_hints_are_preserved(self):
        for error_type, recovery in (
            (StreamResetRequired, "read_stream_recent"),
            (ViewResetRequired, "reset_view"),
            (CursorExpired, "bootstrap"),
            (ReceiptNotFound, "no_committed_receipt_at_lookup; retry_only_with_same_operation_id"),
        ):
            for metadata in ({}, {"recovery": "custom_hint"}):
                with self.subTest(error=error_type.__name__, metadata=metadata):
                    error = error_type("recover", **metadata)
                    if isinstance(error, CursorExpired):
                        error.event_floor = 17
                    payload = error_response(error)[1]
                    self.assertEqual(payload["recovery"], recovery)
                    self.assertFalse(payload["retryable"])
                    if isinstance(error, CursorExpired):
                        self.assertEqual(payload["event_floor"], 17)

    def test_raw_storage_and_unknown_errors_remain_sanitized(self):
        status, payload = error_response(sqlite3.OperationalError("database locked: private context"))
        self.assertEqual(status, 503)
        self.assertEqual(payload, {
            "error": "StorageBusy",
            "message": "world storage is busy; retry the same operation_id",
            "retryable": True,
            "recovery": "retry_same_operation",
        })
        for error in (RuntimeError("private context"), sqlite3.OperationalError("private SQL failed")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(error_response(error), (500, {
                    "error": "InternalError",
                    "message": "world request failed; inspect server logs",
                    "retryable": False,
                    "recovery": "query_receipt_before_retry",
                }))

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
