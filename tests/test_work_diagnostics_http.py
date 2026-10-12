"""Default health remains public; host timings never follow forwarded headers."""
from pathlib import Path
import tempfile
import unittest

import httpx
from agent_world.http_app import create_app


class WorkDiagnosticsHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "world.sqlite3", installer=lambda *_: None)
        self.addCleanup(self.app.state.runtime.close)

    async def health(self, peer, *, diagnostics=False, headers=None):
        transport = httpx.ASGITransport(app=self.app, client=(peer, 43210))
        async with httpx.AsyncClient(transport=transport, base_url="http://world.example") as client:
            return await client.get("/health", params={"diagnostics": str(diagnostics).lower()}, headers=headers)

    async def test_default_health_is_public_and_unchanged(self):
        reply = await self.health("203.0.113.7")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(set(reply.json()), {"ok", "version", "universe", "auth_required"})

    async def test_direct_loopback_can_read_bounded_host_timings(self):
        for peer in ("127.0.0.1", "::1"):
            reply = await self.health(peer, diagnostics=True)
            self.assertEqual(reply.status_code, 200)
            data = reply.json()["work_diagnostics"]
            self.assertEqual(data["capacity"], 128)
            self.assertLessEqual(len(data["recent"]), 32)

    async def test_remote_or_unknown_peer_cannot_claim_loopback_with_headers(self):
        for peer in ("203.0.113.7", "testclient"):
            reply = await self.health(peer, diagnostics=True,
                headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
            self.assertEqual(reply.status_code, 403)
            self.assertEqual(reply.json()["error"], "HostDiagnosticsOnly")
            self.assertNotIn("work_diagnostics", reply.json())
