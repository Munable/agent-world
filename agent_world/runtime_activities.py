from __future__ import annotations
import hashlib
import hmac
import math
import time
from .runtime_contracts import identifier, integer, duration, json_text
from .runtime_errors import ActivityConflict, ActivityNotFound, ClaimBusy, ClaimFenced, InvalidArguments


class RuntimeActivities:
    def start_activity(
        self,
        universe,
        activity_id,
        role_id,
        kind,
        *,
        exclusive_group=None,
        ttl_seconds=None,
        identity_token=None,
    ):
        identifier(activity_id, "activity_id")
        identifier(kind, "kind")
        if exclusive_group is not None:
            identifier(exclusive_group, "exclusive_group")
        if ttl_seconds is not None:
            ttl_seconds = duration(ttl_seconds, "ttl_seconds")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._admit_actor_tx(c, universe, role_id, identity_token)
            c.execute(
                "UPDATE activities SET status='expired' WHERE universe=? AND status='active' "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (universe, now),
            )
            old = c.execute(
                "SELECT * FROM activities WHERE universe=? AND activity_id=?", (universe, activity_id)
            ).fetchone()
            if old:
                old_ttl = old["requested_ttl"]
                same_ttl = old_ttl is None and ttl_seconds is None
                if old_ttl is not None and ttl_seconds is not None:
                    same_ttl = math.isclose(float(old_ttl), ttl_seconds, rel_tol=1e-9, abs_tol=1e-6)
                if (
                    old["role_id"] != role_id
                    or old["kind"] != kind
                    or old["exclusive_group"] != exclusive_group
                    or not same_ttl
                ):
                    raise ActivityConflict("activity_id belongs to a different activity request")
                if old["status"] != "active":
                    raise ActivityConflict("activity is no longer active")
                return {
                    "activity_id": activity_id,
                    "universe": universe,
                    "role_id": role_id,
                    "kind": kind,
                    "exclusive_group": exclusive_group,
                    "expires_at": old["expires_at"],
                    "replayed": True,
                }
            if (
                exclusive_group
                and c.execute(
                    "SELECT 1 FROM activities WHERE universe=? AND role_id=? AND exclusive_group=? AND status='active'",
                    (universe, role_id, exclusive_group),
                ).fetchone()
            ):
                raise ActivityConflict("an activity already occupies this role's exclusive group")
            expires = now + ttl_seconds if ttl_seconds is not None else None
            c.execute(
                "INSERT INTO activities(universe,activity_id,role_id,kind,exclusive_group,status,expires_at,created_at,requested_ttl) "
                "VALUES(?,?,?,?,?,'active',?,?,?)",
                (universe, activity_id, role_id, kind, exclusive_group, expires, now, ttl_seconds),
            )
        self.wake_waiters()
        return {
            "activity_id": activity_id,
            "universe": universe,
            "role_id": role_id,
            "kind": kind,
            "exclusive_group": exclusive_group,
            "expires_at": expires,
            "replayed": False,
        }

    def _expire_activity_tx(self, c, universe, activity_id, now):
        c.execute(
            "UPDATE activities SET status='expired' WHERE universe=? AND activity_id=? AND status='active' "
            "AND expires_at IS NOT NULL AND expires_at<=?",
            (universe, activity_id, now),
        )

    def claim_activity(
        self, universe, activity_id, role_id, runtime_id, *, lease_seconds=30.0, identity_token=None
    ):
        identifier(activity_id, "activity_id")
        identifier(runtime_id, "runtime_id")
        lease_seconds = duration(lease_seconds, "lease_seconds", 3600)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._admit_actor_tx(c, universe, role_id, identity_token)
            self._expire_activity_tx(c, universe, activity_id, now)
            activity = c.execute(
                "SELECT * FROM activities WHERE universe=? AND activity_id=?", (universe, activity_id)
            ).fetchone()
            if activity is None:
                raise ActivityNotFound(activity_id)
            if activity["role_id"] != role_id or activity["status"] != "active":
                raise ClaimFenced("activity is inactive or owned by another role")
            old = c.execute(
                "SELECT * FROM activity_claims WHERE universe=? AND activity_id=?", (universe, activity_id)
            ).fetchone()
            replayed = bool(old and old["claim_until"] > now)
            if replayed:
                if old["runtime_id"] != runtime_id:
                    raise ClaimBusy("activity is held by another runtime")
                epoch, until = int(old["claim_epoch"]), float(old["claim_until"])
            else:
                epoch = int(old["claim_epoch"]) + 1 if old else 1
                until = now + lease_seconds
                if activity["expires_at"] is not None:
                    until = min(until, float(activity["expires_at"]))
            token = self._claim_token(c, universe, activity_id, role_id, runtime_id, epoch)
            digest = hashlib.sha256(token.encode()).hexdigest()
            if replayed and not hmac.compare_digest(digest, old["claim_token_hash"]):
                raise ClaimFenced("stored claim proof is inconsistent")
            if not replayed:
                c.execute(
                    "INSERT INTO activity_claims(universe,activity_id,role_id,runtime_id,claim_epoch,claim_token_hash,claim_until,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(universe,activity_id) DO UPDATE SET "
                    "role_id=excluded.role_id,runtime_id=excluded.runtime_id,claim_epoch=excluded.claim_epoch,"
                    "claim_token_hash=excluded.claim_token_hash,claim_until=excluded.claim_until,updated_at=excluded.updated_at",
                    (universe, activity_id, role_id, runtime_id, epoch, digest, until, now),
                )
        return {
            "activity_id": activity_id,
            "runtime_id": runtime_id,
            "claim_epoch": epoch,
            "claim_token": token,
            "claim_until": until,
            "replayed": replayed,
        }

    def _validate_claim_tx(self, c, universe, role_id, proof, now):
        if not isinstance(proof, dict):
            raise ClaimFenced("activity claim proof must be an object")
        try:
            for key in ("activity_id", "runtime_id", "claim_token"):
                identifier(proof.get(key), key)
            integer(proof.get("claim_epoch"), "claim_epoch", 1)
        except InvalidArguments as exc:
            raise ClaimFenced("activity claim proof is incomplete or malformed") from exc
        activity_id = proof["activity_id"]
        activity = c.execute(
            "SELECT * FROM activities WHERE universe=? AND activity_id=?", (universe, activity_id)
        ).fetchone()
        if (
            activity is None
            or activity["role_id"] != role_id
            or activity["status"] != "active"
            or (activity["expires_at"] is not None and activity["expires_at"] <= now)
        ):
            raise ClaimFenced("activity is inactive or owned by another role")
        claim = c.execute(
            "SELECT * FROM activity_claims WHERE universe=? AND activity_id=?", (universe, activity_id)
        ).fetchone()
        digest = hashlib.sha256(proof["claim_token"].encode()).hexdigest()
        if (
            claim is None
            or claim["role_id"] != role_id
            or claim["runtime_id"] != proof["runtime_id"]
            or claim["claim_epoch"] != proof["claim_epoch"]
            or claim["claim_until"] <= now
            or not hmac.compare_digest(claim["claim_token_hash"], digest)
        ):
            raise ClaimFenced("claim is stale, expired, or owned by another runtime")
        return activity_id

    def _control_receipt_tx(
        self, c, universe, role_id, name, operation_id, arguments, result, activity_id, event_kind=None
    ):
        now = time.time()
        events = []
        if event_kind:
            cursor = c.execute(
                "INSERT INTO events(universe,recipient_role_id,actor_role_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (universe, role_id, role_id, event_kind, json_text(result), now),
            )
            events.append(cursor.lastrowid)
        receipt = {
            "ok": True,
            "operation_id": operation_id,
            "function_id": name,
            "function_version": 1,
            "result": result,
            "event_seqs": events,
            "replayed": False,
        }
        c.execute(
            "INSERT INTO operations(universe,actor_role_id,idempotency_key,function_id,function_version,args_hash,receipt_json,created_at,activity_id) "
            "VALUES(?,?,?,?,1,?,?,?,?)",
            (
                universe,
                role_id,
                operation_id,
                name,
                self._sha256({"function_id": name, "arguments": arguments}),
                json_text(receipt),
                now,
                activity_id,
            ),
        )
        return receipt

    def renew_claim(self, universe, role_id, proof, *, operation_id, lease_seconds=30.0, identity_token=None):
        identifier(operation_id, "operation_id")
        lease_seconds = duration(lease_seconds, "lease_seconds", 3600)
        args = {
            "activity_id": proof.get("activity_id") if isinstance(proof, dict) else None,
            "lease_seconds": lease_seconds,
        }
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            old = c.execute(
                "SELECT * FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                (universe, role_id, operation_id),
            ).fetchone()
            if old:
                return self._replay_tx(old, "world.renew_claim", args, proof)
            now = time.time()
            activity_id = self._validate_claim_tx(c, universe, role_id, proof, now)
            activity = c.execute(
                "SELECT expires_at FROM activities WHERE universe=? AND activity_id=?",
                (universe, activity_id),
            ).fetchone()
            old_until = c.execute(
                "SELECT claim_until FROM activity_claims WHERE universe=? AND activity_id=?",
                (universe, activity_id),
            ).fetchone()[0]
            until = max(old_until, now + lease_seconds)
            if activity["expires_at"] is not None:
                until = min(until, activity["expires_at"])
            c.execute(
                "UPDATE activity_claims SET claim_until=?,updated_at=? WHERE universe=? AND activity_id=?",
                (until, now, universe, activity_id),
            )
            result = {key: proof[key] for key in ("activity_id", "runtime_id", "claim_epoch", "claim_token")}
            result["claim_until"] = until
            return self._control_receipt_tx(
                c, universe, role_id, "world.renew_claim", operation_id, args, result, activity_id
            )

    def finish_activity(
        self, universe, role_id, proof, *, operation_id, status="completed", identity_token=None
    ):
        identifier(operation_id, "operation_id")
        if status not in {"completed", "cancelled"}:
            raise InvalidArguments("finish status must be completed or cancelled")
        args = {
            "activity_id": proof.get("activity_id") if isinstance(proof, dict) else None,
            "status": status,
        }
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            old = c.execute(
                "SELECT * FROM operations WHERE universe=? AND actor_role_id=? AND idempotency_key=?",
                (universe, role_id, operation_id),
            ).fetchone()
            if old:
                return self._replay_tx(old, "world.finish_activity", args, proof)
            activity_id = self._validate_claim_tx(c, universe, role_id, proof, time.time())
            c.execute(
                "UPDATE activities SET status=? WHERE universe=? AND activity_id=?",
                (status, universe, activity_id),
            )
            c.execute(
                "UPDATE activity_claims SET claim_until=0 WHERE universe=? AND activity_id=?",
                (universe, activity_id),
            )
            result = {"activity_id": activity_id, "status": status}
            receipt = self._control_receipt_tx(
                c,
                universe,
                role_id,
                "world.finish_activity",
                operation_id,
                args,
                result,
                activity_id,
                "activity_finished",
            )
        self.wake_waiters()
        return receipt
