from __future__ import annotations

import pathlib
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from agent_world.http_app import create_app


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = pathlib.Path(tmp) / "read-http.sqlite3"
        app = create_app(db, "demo", auth_required=True)
        runtime = app.state.runtime
        token = runtime.issue_identity_token("demo", "READ-ROLE", ttl_seconds=60)
        headers = {"Authorization": f"Bearer {token['token']}"}
        client = TestClient(app)

        write = client.post(
            "/v1/functions/counter.increment/invoke",
            headers=headers,
            json={
                "operation_id": "read-http-write-1",
                "arguments": {"amount": 6},
            },
        )
        assert write.status_code == 200, write.text
        assert write.json()["result"]["value"] == 6

        read = client.post(
            "/v1/functions/counter.get/invoke",
            headers=headers,
            json={"arguments": {}},
        )
        assert read.status_code == 200, read.text
        assert read.json()["result"]["value"] == 6
        assert read.json()["read_only"] is True

        missing = client.post(
            "/v1/functions/counter.increment/invoke",
            headers=headers,
            json={"arguments": {"amount": 1}},
        )
        assert missing.status_code == 409, missing.text
        assert missing.json()["error"] == "OperationConflict"

        with sqlite3.connect(db) as conn:
            operations = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert operations == 1
        assert events == 1

        client.close()
        print("PASS http_read_without_operation_id")
        print("PASS http_write_still_requires_operation_id")
        print("PASS http_read_created_no_receipt_or_event")
        print("V06 READ HTTP: 3/3 passed")


if __name__ == "__main__":
    main()
