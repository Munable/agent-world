from __future__ import annotations

import unittest

import httpx

from agent_world import WorldRuntime
from tests.fixtures.error_metadata_world import ERROR_TYPES, WORLD
from tests.live_server import LiveServer


class ErrorMetadataTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_special_errors_preserve_metadata_from_python_to_http_and_mcp(self):
        with LiveServer("tests.fixtures.error_metadata_world:WORLD") as server:
            role, identity = server.role("Caller")
            runtime = WorldRuntime(server.db)
            runtime.install_world("network", WORLD)
            auth = {"Authorization": "Bearer " + identity["token"]}

            async with server.session(identity) as (session, _):
                async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                    for name, status, recovery in (
                        ("StreamResetRequired", 409, "read_stream_recent"),
                        ("ViewResetRequired", 409, "reset_view"),
                        ("ViewNotFound", 404, "retry_after_condition"),
                        ("TimerConflict", 409, "retry_after_condition"),
                        ("TimerNotFound", 404, "retry_after_condition"),
                        ("StorageBusy", 503, "retry_after_condition"),
                    ):
                        with self.subTest(error=name):
                            args = {"error": name}
                            with self.assertRaises(ERROR_TYPES[name]) as raised:
                                runtime.query_function(
                                    "network", "error.fail", role["role_id"], args,
                                    identity_token=identity["token"],
                                )
                            error = raised.exception
                            self.assertEqual(str(error), "public failure context")
                            self.assertEqual(error.retryable, name != "StorageBusy")
                            self.assertEqual(error.recovery, "retry_after_condition")
                            self.assertEqual(error.retry_after_seconds, 1.25)
                            self.assertEqual(error.details, {
                                "scope": "probe", "remaining": 0, "context": [None, True],
                            })
                            expected = {
                                "error": name,
                                "message": str(error),
                                "retryable": error.retryable,
                                "recovery": recovery,
                                "retry_after_seconds": error.retry_after_seconds,
                                "details": error.details,
                            }
                            response = await client.post(
                                server.url + "/v1/functions/error.fail/invoke",
                                headers=auth,
                                json={"arguments": args},
                            )
                            self.assertEqual(response.status_code, status, response.text)
                            self.assertEqual(response.json(), expected)
                            self.assertEqual(response.headers["retry-after"], "2")

                            result = await session.call_tool("error.fail", arguments={"arguments": args})
                            self.assertTrue(result.is_error)
                            self.assertEqual(result.structured_content, {**expected, "tool": "error.fail"})


if __name__ == "__main__":
    unittest.main()
