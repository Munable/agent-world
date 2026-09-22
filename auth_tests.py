from __future__ import annotations

import pathlib
import tempfile
import time

from fastapi.testclient import TestClient

from http_app import create_app
from runtime_core import InvalidIdentityToken


def expect_status(response, status: int):
    assert response.status_code == status, response.text
    return response.json()


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "auth.sqlite3"
        app = create_app(db, "demo", auth_required=True)
        runtime = app.state.runtime
        client = TestClient(app)

        issued = runtime.issue_identity_token("demo", "ROLE-A", ttl_seconds=60)
        token = issued["token"]
        headers = {"Authorization": f"Bearer {token}"}

        health = expect_status(client.get("/health"), 200)
        assert health["auth_required"] is True
        expect_status(client.get("/v1/functions"), 401)
        expect_status(
            client.get(
                "/v1/functions",
                headers={"Authorization": "Bearer awid_not-a-real-token"},
            ),
            401,
        )

        who = expect_status(client.get("/v1/whoami", headers=headers), 200)
        assert who["role_id"] == "ROLE-A"
        assert who["universe"] == "demo"

        functions = expect_status(client.get("/v1/functions", headers=headers), 200)
        assert "counter.increment" in {
            row["function_id"] for row in functions["functions"]
        }

        boot = expect_status(client.get("/v1/bootstrap", headers=headers), 200)
        assert boot["role_id"] == "ROLE-A"

        invoke = expect_status(
            client.post(
                "/v1/functions/counter.increment/invoke",
                headers=headers,
                json={
                    "operation_id": "auth-op-1",
                    "arguments": {"amount": 2},
                },
            ),
            200,
        )
        assert invoke["result"]["value"] == 2

        spoof = expect_status(
            client.post(
                "/v1/functions/counter.increment/invoke",
                headers=headers,
                json={
                    "role_id": "ROLE-B",
                    "operation_id": "auth-op-spoof",
                    "arguments": {"amount": 9},
                },
            ),
            403,
        )
        assert spoof["error"] == "IdentityScopeMismatch"
        assert runtime.get_state("demo", "role:ROLE-B", "counter", 0)["value"] == 0

        other = runtime.issue_identity_token("other", "ROLE-A", ttl_seconds=60)
        expect_status(
            client.get(
                "/v1/functions",
                headers={"Authorization": f"Bearer {other['token']}"},
            ),
            403,
        )

        expiring = runtime.issue_identity_token("demo", "ROLE-X", ttl_seconds=0.05)
        time.sleep(0.08)
        try:
            runtime.resolve_identity_token(expiring["token"])
        except InvalidIdentityToken:
            pass
        else:
            raise AssertionError("expired identity token was accepted")

        revokable = runtime.issue_identity_token("demo", "ROLE-R", ttl_seconds=60)
        assert runtime.revoke_identity_token(revokable["token_id"]) is True
        expect_status(
            client.get(
                "/v1/functions",
                headers={"Authorization": f"Bearer {revokable['token']}"},
            ),
            401,
        )

        client.close()
        print("PASS auth_required_http_identity_binding")
        print("PASS role_spoof_rejected_before_world_mutation")
        print("PASS token_scope_expiry_and_revocation")


if __name__ == "__main__":
    main()
