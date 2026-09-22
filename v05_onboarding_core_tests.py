from __future__ import annotations

import concurrent.futures
import pathlib
import sqlite3
import tempfile
import time

from demo_universe import install_demo_universe
from runtime_core import (
    InvalidIdentityToken,
    JoinTicketConsumed,
    JoinTicketExpired,
    RoleInactive,
    WorldRuntime,
)


def fresh():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = pathlib.Path(tmp.name) / "v05.sqlite3"
    runtime = WorldRuntime(db)
    install_demo_universe(runtime, "demo")
    return tmp, db, runtime


def test_role_core_and_bootstrap():
    tmp, _, runtime = fresh()
    try:
        role = runtime.create_role("Mubin Agent", "avatar://default")
        assert role["role_id"].startswith("awr_")
        assert role["display_name"] == "Mubin Agent"
        updated = runtime.update_role_profile(
            role["role_id"],
            display_name="Mubin Agent 2",
            avatar_ref=None,
        )
        assert updated["display_name"] == "Mubin Agent 2"
        assert updated["avatar_ref"] is None
        boot = runtime.bootstrap("demo", role["role_id"])
        assert boot["role_profile"]["role_id"] == role["role_id"]
        assert boot["role_profile"]["display_name"] == "Mubin Agent 2"
        assert boot["world_entry_state"]["latest_event_seq"] == boot["latest_event_seq"]

        legacy = runtime.bootstrap("demo", "LEGACY-A")
        assert legacy["role_profile"] is None
        print("PASS role_core_and_bootstrap")
    finally:
        tmp.cleanup()


def test_ticket_exchange_and_plaintext_not_stored():
    tmp, db, runtime = fresh()
    try:
        role = runtime.create_role("Ticket Role")
        issued = runtime.issue_join_ticket("demo", role["role_id"], ttl_seconds=60)
        assert issued["ticket"].startswith("awjt_")

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT ticket_hash FROM join_tickets WHERE ticket_id=?",
                (issued["ticket_id"],),
            ).fetchone()
        assert row is not None
        assert row[0] != issued["ticket"]
        assert issued["ticket"] not in row[0]

        identity = runtime.exchange_join_ticket(issued["ticket"])
        assert identity["token"].startswith("awid_")
        assert identity["role_profile"]["role_id"] == role["role_id"]
        resolved = runtime.resolve_identity_token(identity["token"])
        assert resolved["role_id"] == role["role_id"]
        assert resolved["universe"] == "demo"
        meta = runtime.get_join_ticket_metadata(issued["ticket_id"])
        assert meta["used_token_id"] == identity["token_id"]
        assert meta["used_at"] is not None

        replay = runtime.exchange_join_ticket(issued["ticket"])
        assert replay["replayed"] is True
        assert replay["token_id"] == identity["token_id"]
        assert replay["token"] == identity["token"]

        with sqlite3.connect(db) as conn:
            stored = conn.execute(
                "SELECT token_hash FROM identity_tokens WHERE token_id=?",
                (identity["token_id"],),
            ).fetchone()[0]
        assert stored != identity["token"]
        assert identity["token"] not in stored
        print("PASS ticket_exchange_and_plaintext_not_stored")
    finally:
        tmp.cleanup()


def test_expiry_and_disabled_role():
    tmp, _, runtime = fresh()
    try:
        role = runtime.create_role("Expiring Role")
        expired = runtime.issue_join_ticket(
            "demo", role["role_id"], ttl_seconds=0.05
        )
        time.sleep(0.08)
        try:
            runtime.exchange_join_ticket(expired["ticket"])
        except JoinTicketExpired:
            pass
        else:
            raise AssertionError("expired join ticket was accepted")

        live = runtime.issue_join_ticket("demo", role["role_id"], ttl_seconds=60)
        runtime.set_role_status(role["role_id"], "disabled")
        try:
            runtime.exchange_join_ticket(live["ticket"])
        except RoleInactive:
            pass
        else:
            raise AssertionError("disabled role exchanged a join ticket")

        runtime.set_role_status(role["role_id"], "active")
        access_ticket = runtime.issue_join_ticket(
            "demo", role["role_id"], ttl_seconds=60
        )
        identity = runtime.exchange_join_ticket(access_ticket["ticket"])
        runtime.set_role_status(role["role_id"], "disabled")
        try:
            runtime.resolve_identity_token(identity["token"])
        except InvalidIdentityToken:
            pass
        else:
            raise AssertionError("disabled role kept authenticated access")
        try:
            runtime.rotate_identity_token(identity["token_id"])
        except InvalidIdentityToken:
            pass
        else:
            raise AssertionError("disabled role rotated an identity token")

        runtime.set_role_status(role["role_id"], "active")
        assert runtime.resolve_identity_token(identity["token"])["role_id"] == role["role_id"]
        print("PASS expiry_and_disabled_role")
    finally:
        tmp.cleanup()
def test_concurrent_single_use_ticket():
    tmp, _, runtime = fresh()
    try:
        role = runtime.create_role("Concurrent Role")
        issued = runtime.issue_join_ticket("demo", role["role_id"], ttl_seconds=60)

        def exchange(_):
            local = WorldRuntime(runtime.db_path)
            result = local.exchange_join_ticket(issued["ticket"])
            return (
                "replay" if result["replayed"] else "first",
                result["token_id"],
                result["token"],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            rows = list(pool.map(exchange, range(12)))

        first = [row for row in rows if row[0] == "first"]
        replay = [row for row in rows if row[0] == "replay"]
        assert len(first) == 1, rows
        assert len(replay) == 11, rows
        assert len({row[1] for row in rows}) == 1, rows
        assert len({row[2] for row in rows}) == 1, rows
        meta = runtime.get_join_ticket_metadata(issued["ticket_id"])
        assert meta["used_token_id"] == first[0][1]
        print("PASS concurrent_single_use_ticket")
    finally:
        tmp.cleanup()


def test_token_rotation():
    tmp, _, runtime = fresh()
    try:
        role = runtime.create_role("Rotate Role")
        ticket = runtime.issue_join_ticket("demo", role["role_id"])
        first = runtime.exchange_join_ticket(ticket["ticket"])
        second = runtime.rotate_identity_token(first["token_id"], ttl_seconds=60)
        assert second["rotated_from_token_id"] == first["token_id"]
        assert second["role_id"] == role["role_id"]
        assert second["token"] != first["token"]

        try:
            runtime.resolve_identity_token(first["token"])
        except InvalidIdentityToken:
            pass
        else:
            raise AssertionError("rotated old token remained valid")

        resolved = runtime.resolve_identity_token(second["token"])
        assert resolved["token_id"] == second["token_id"]
        old_meta = runtime.get_identity_token_metadata(first["token_id"])
        assert old_meta["revoked_at"] is not None

        try:
            runtime.exchange_join_ticket(ticket["ticket"])
        except JoinTicketConsumed:
            pass
        else:
            raise AssertionError("rotated credential was resurrected by old join ticket")

        print("PASS token_rotation")
    finally:
        tmp.cleanup()


def main():
    test_role_core_and_bootstrap()
    test_ticket_exchange_and_plaintext_not_stored()
    test_expiry_and_disabled_role()
    test_concurrent_single_use_ticket()
    test_token_rotation()
    print("V05 CORE: 5/5 passed")


if __name__ == "__main__":
    main()
