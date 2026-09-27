from __future__ import annotations

import unittest

import httpx

from tests.live_server import LiveServer


class StructuredErrorTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_world_error_metadata_survives_http_and_mcp(self):
        with LiveServer(
            "tests.fixtures.capability_matrix_world:WORLD"
        ) as server:
            _, identity = server.role("Caller")
            auth = {
                "Authorization": "Bearer " + identity["token"]
            }

            with httpx.Client(
                trust_env=False,
                timeout=10,
            ) as client:
                response = client.post(
                    server.url
                    + "/v1/functions/topology.structured_failure/invoke",
                    headers=auth,
                    json={
                        "operation_id": "http-failure",
                        "arguments": {},
                    },
                )
                self.assertEqual(
                    response.status_code,
                    409,
                    response.text,
                )
                self.assertEqual(
                    response.headers["retry-after"],
                    "2",
                )
                http_error = response.json()

            expected = {
                "error": "RuleViolation",
                "message": "probe capacity reached",
                "retryable": True,
                "recovery": "retry_after_condition",
                "retry_after_seconds": 1.25,
                "details": {
                    "scope": "capability_probe",
                    "limit": 3,
                    "used": 3,
                    "remaining": 0,
                    "reset_condition": "external_signal",
                },
            }
            self.assertEqual(http_error, expected)

            async with server.session(identity) as (
                session,
                _,
            ):
                result = await session.call_tool(
                    "topology.structured_failure",
                    arguments={
                        "operation_id": "mcp-failure",
                        "arguments": {},
                    },
                )
                self.assertTrue(result.is_error)
                mcp_error = dict(result.structured_content)
                self.assertEqual(
                    mcp_error.pop("tool"),
                    "topology.structured_failure",
                )
                self.assertEqual(mcp_error, expected)

    async def test_auth_and_request_errors_offer_recovery(self):
        with LiveServer(
            "tests.fixtures.capability_matrix_world:WORLD"
        ) as server:
            _, identity = server.role("Caller")

            with httpx.Client(
                trust_env=False,
                timeout=10,
            ) as client:
                missing = client.get(
                    server.url + "/v1/bootstrap"
                )
                self.assertEqual(
                    missing.status_code,
                    401,
                )
                self.assertEqual(
                    missing.json()["recovery"],
                    "authenticate",
                )

                invalid = client.post(
                    server.url
                    + "/v1/functions/topology.broadcast/invoke",
                    headers={
                        "Authorization":
                        "Bearer " + identity["token"]
                    },
                    json={
                        "operation_id": "bad-shape",
                        "arguments": {"recipients": []},
                    },
                )
                self.assertEqual(
                    invalid.status_code,
                    422,
                )
                self.assertEqual(
                    invalid.json()["recovery"],
                    "fix_request",
                )


if __name__ == "__main__":
    unittest.main()
