from __future__ import annotations

import pathlib
import tempfile

from fastapi.testclient import TestClient

from agent_world.http_app import create_app
from agent_world.onboarding_app import create_onboarding_app


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "api.sqlite3"
        operator = "test-operator-secret"
        onboarding = create_onboarding_app(
            db,
            "demo",
            mcp_url="https://world.example/mcp",
            public_base_url="https://world.example",
            operator_key=operator,
        )
        client = TestClient(onboarding)
        op_headers = {"X-Operator-Key": operator}

        assert client.post(
            "/v1/roles",
            json={"display_name": "No Auth"},
        ).status_code == 401
        assert client.post(
            "/v1/roles",
            headers={"X-Operator-Key": "wrong"},
            json={"display_name": "Wrong Auth"},
        ).status_code == 403
        role_response = client.post(
            "/v1/roles",
            headers=op_headers,
            json={
                "display_name": "World Walker",
                "avatar_ref": "avatar://walker",
            },
        )
        assert role_response.status_code == 200, role_response.text
        role = role_response.json()
        assert role["role_id"].startswith("awr_")

        update = client.patch(
            f"/v1/roles/{role['role_id']}",
            headers=op_headers,
            json={"display_name": "World Walker II", "avatar_ref": None},
        )
        assert update.status_code == 200
        assert update.json()["display_name"] == "World Walker II"
        assert update.json()["avatar_ref"] is None

        package_response = client.post(
            "/v1/join-tickets",
            headers=op_headers,
            json={"role_id": role["role_id"], "ttl_seconds": 60},
        )
        assert package_response.status_code == 200, package_response.text
        package = package_response.json()
        assert package["version"] == "agent-world-join-v1"
        assert package["join"]["exchange_url"] == "https://world.example/v1/join/exchange"
        assert package["mcp"]["url"] == "https://world.example/mcp"
        ticket = package["join"]["ticket"]
        exchange = client.post(
            "/v1/join/exchange",
            json={"ticket": ticket},
        )
        assert exchange.status_code == 200, exchange.text
        identity_package = exchange.json()
        assert identity_package["replayed"] is False
        token = identity_package["identity"]["token"]
        token_id = identity_package["identity"]["token_id"]
        assert token.startswith("awid_")
        assert identity_package["next"]["tool"] == "world.bootstrap"
        assert identity_package["next"]["arguments"] == {}

        replay = client.post(
            "/v1/join/exchange",
            json={"ticket": ticket},
        )
        assert replay.status_code == 200
        replay_body = replay.json()
        assert replay_body["replayed"] is True
        assert replay_body["identity"]["token"] == token
        assert replay_body["identity"]["token_id"] == token_id

        world_app = create_app(db, "demo", auth_required=True)
        world = TestClient(world_app)
        auth = {"Authorization": f"Bearer {token}"}
        boot = world.get("/v1/bootstrap", headers=auth)
        assert boot.status_code == 200, boot.text
        boot_body = boot.json()
        assert boot_body["role_id"] == role["role_id"]
        assert boot_body["role_profile"]["display_name"] == "World Walker II"
        mutation = world.post(
            "/v1/functions/counter.increment/invoke",
            headers=auth,
            json={
                "operation_id": "onboarding-api-op-1",
                "arguments": {"amount": 2},
            },
        )
        assert mutation.status_code == 200, mutation.text
        assert mutation.json()["result"]["value"] == 2

        rotated = client.post(
            f"/v1/identity-tokens/{token_id}/rotate",
            headers=op_headers,
            json={"ttl_seconds": 60},
        )
        assert rotated.status_code == 200, rotated.text
        rotated_body = rotated.json()
        new_token = rotated_body["token"]
        new_token_id = rotated_body["token_id"]
        assert new_token != token

        assert world.get("/v1/bootstrap", headers=auth).status_code == 401
        new_auth = {"Authorization": f"Bearer {new_token}"}
        assert world.get("/v1/bootstrap", headers=new_auth).status_code == 200

        revoked = client.delete(
            f"/v1/identity-tokens/{new_token_id}",
            headers=op_headers,
        )
        assert revoked.status_code == 200
        assert world.get("/v1/bootstrap", headers=new_auth).status_code == 401

        world.close()
        client.close()
        print("PASS onboarding_operator_boundary")
        print("PASS connection_package_and_idempotent_exchange")
        print("PASS exchanged_token_enters_authenticated_world")
        print("PASS rotate_and_revoke_lifecycle")
        print("V05 API: 4/4 passed")


if __name__ == "__main__":
    main()
