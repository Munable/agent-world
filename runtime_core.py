from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class WorldRuntimeError(RuntimeError):
    pass


class RegistryConflict(WorldRuntimeError):
    pass


class FunctionNotFound(WorldRuntimeError):
    pass


class FunctionVersionMismatch(WorldRuntimeError):
    pass


class SchemaRejected(WorldRuntimeError):
    pass


class OperationConflict(WorldRuntimeError):
    pass


class ActivityConflict(WorldRuntimeError):
    pass


class ActivityNotFound(WorldRuntimeError):
    pass


class ClaimBusy(WorldRuntimeError):
    pass


class ClaimFenced(WorldRuntimeError):
    pass


class CursorExpired(WorldRuntimeError):
    pass


class AuthenticationRequired(WorldRuntimeError):
    pass


class InvalidIdentityToken(WorldRuntimeError):
    pass


class IdentityScopeMismatch(WorldRuntimeError):
    pass


class RoleNotFound(WorldRuntimeError):
    pass


class RoleInvalid(WorldRuntimeError):
    pass


class RoleInactive(WorldRuntimeError):
    pass


class JoinTicketInvalid(WorldRuntimeError):
    pass


class JoinTicketExpired(WorldRuntimeError):
    pass


class JoinTicketConsumed(WorldRuntimeError):
    pass


_UNSET = object()


@dataclass(frozen=True)
class EventSpec:
    recipient_role_id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class FunctionOutcome:
    result: dict[str, Any]
    events: tuple[EventSpec, ...] = ()


class FunctionContext:
    def __init__(
        self,
        conn: sqlite3.Connection,
        universe: str,
        actor_role_id: str,
        function_id: str,
        function_version: int,
        activity_id: str | None = None,
        access: str = "write",
    ):
        self.conn = conn
        self.universe = universe
        self.actor_role_id = actor_role_id
        self.function_id = function_id
        self.function_version = function_version
        self.activity_id = activity_id
        self.access = access

    def get_state(self, scope: str, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            """SELECT value_json FROM world_state
               WHERE universe=? AND scope=? AND state_key=?""",
            (self.universe, scope, key),
        ).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def set_state(self, scope: str, key: str, value: Any) -> int:
        if self.access == "read":
            raise WorldRuntimeError("read function cannot modify world state")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        row = self.conn.execute(
            """SELECT version FROM world_state
               WHERE universe=? AND scope=? AND state_key=?""",
            (self.universe, scope, key),
        ).fetchone()
        next_version = (int(row["version"]) + 1) if row else 1
        self.conn.execute(
            """INSERT INTO world_state(universe,scope,state_key,value_json,version,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(universe,scope,state_key)
               DO UPDATE SET value_json=excluded.value_json,
                             version=excluded.version,
                             updated_at=excluded.updated_at""",
            (self.universe, scope, key, encoded, next_version, now),
        )
        return next_version


FunctionHandler = Callable[[FunctionContext, dict[str, Any]], FunctionOutcome]


class WorldRuntime:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._handlers: dict[tuple[str, str, int], FunctionHandler] = {}
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_meta(
                  meta_key TEXT PRIMARY KEY,
                  meta_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS function_registry(
                  universe TEXT NOT NULL,
                  function_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  description TEXT NOT NULL,
                  input_schema_json TEXT NOT NULL,
                  access TEXT NOT NULL,
                  availability TEXT NOT NULL,
                  requires_activity_claim INTEGER NOT NULL,
                  descriptor_hash TEXT NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(universe,function_id)
                );
                CREATE TABLE IF NOT EXISTS operations(
                  universe TEXT NOT NULL,
                  actor_role_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  function_id TEXT NOT NULL,
                  function_version INTEGER NOT NULL,
                  args_hash TEXT NOT NULL,
                  receipt_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  PRIMARY KEY(universe,actor_role_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS world_state(
                  universe TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  state_key TEXT NOT NULL,
                  value_json TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(universe,scope,state_key)
                );
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  universe TEXT NOT NULL,
                  recipient_role_id TEXT NOT NULL,
                  actor_role_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_floors(
                  universe TEXT PRIMARY KEY,
                  floor INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activities(
                  universe TEXT NOT NULL,
                  activity_id TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  exclusive_group TEXT,
                  status TEXT NOT NULL,
                  expires_at REAL,
                  created_at REAL NOT NULL,
                  PRIMARY KEY(universe,activity_id)
                );
                CREATE TABLE IF NOT EXISTS activity_claims(
                  universe TEXT NOT NULL,
                  activity_id TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  runtime_id TEXT NOT NULL,
                  claim_epoch INTEGER NOT NULL,
                  claim_token_hash TEXT NOT NULL,
                  claim_until REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(universe,activity_id)
                );
                CREATE TABLE IF NOT EXISTS roles(
                  role_id TEXT PRIMARY KEY,
                  display_name TEXT NOT NULL,
                  avatar_ref TEXT,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS join_tickets(
                  ticket_id TEXT PRIMARY KEY,
                  ticket_hash TEXT NOT NULL UNIQUE,
                  universe TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  used_at REAL,
                  used_token_id TEXT,
                  FOREIGN KEY(role_id) REFERENCES roles(role_id)
                );
                CREATE INDEX IF NOT EXISTS idx_join_tickets_scope
                  ON join_tickets(universe,role_id);
                CREATE TABLE IF NOT EXISTS identity_tokens(
                  token_id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL UNIQUE,
                  universe TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  expires_at REAL,
                  revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_identity_tokens_scope
                  ON identity_tokens(universe,role_id);
                CREATE TABLE IF NOT EXISTS role_world_presence(
                  universe TEXT NOT NULL,
                  role_id TEXT NOT NULL,
                  first_bootstrap_at REAL NOT NULL,
                  last_bootstrap_at REAL NOT NULL,
                  bootstrap_count INTEGER NOT NULL,
                  PRIMARY KEY(universe,role_id)
                );
                """
            )
            row = c.execute(
                "SELECT meta_value FROM runtime_meta WHERE meta_key='claim_secret'"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO runtime_meta(meta_key,meta_value) VALUES('claim_secret',?)",
                    (secrets.token_hex(32),),
                )
            row = c.execute(
                "SELECT meta_value FROM runtime_meta WHERE meta_key='identity_secret'"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO runtime_meta(meta_key,meta_value) VALUES('identity_secret',?)",
                    (secrets.token_hex(32),),
                )

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _sha256(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical(value).encode("utf-8")).hexdigest()

    def _claim_secret(self, conn: sqlite3.Connection) -> bytes:
        row = conn.execute(
            "SELECT meta_value FROM runtime_meta WHERE meta_key='claim_secret'"
        ).fetchone()
        if row is None:
            raise WorldRuntimeError("claim secret missing")
        return bytes.fromhex(row["meta_value"])

    def _identity_secret(self, conn: sqlite3.Connection) -> bytes:
        row = conn.execute(
            "SELECT meta_value FROM runtime_meta WHERE meta_key='identity_secret'"
        ).fetchone()
        if row is None:
            raise WorldRuntimeError("identity secret missing")
        return bytes.fromhex(row["meta_value"])

    def _claim_token(
        self,
        conn: sqlite3.Connection,
        universe: str,
        activity_id: str,
        role_id: str,
        runtime_id: str,
        epoch: int,
    ) -> str:
        message = "\x00".join(
            ["claim-v1", universe, activity_id, role_id, runtime_id, str(epoch)]
        ).encode("utf-8")
        digest = hmac.new(self._claim_secret(conn), message, hashlib.sha256).hexdigest()
        return "awc_" + digest

    @staticmethod
    def _normalize_role_profile(
        display_name: str,
        avatar_ref: str | None,
    ) -> tuple[str, str | None]:
        name = str(display_name).strip()
        if not name or len(name) > 80:
            raise RoleInvalid("display_name must be 1..80 characters")
        avatar = None if avatar_ref is None else str(avatar_ref).strip()
        if avatar == "":
            avatar = None
        if avatar is not None and len(avatar) > 1024:
            raise RoleInvalid("avatar_ref must be at most 1024 characters")
        return name, avatar

    @staticmethod
    def _row_to_role(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "role_id": row["role_id"],
            "display_name": row["display_name"],
            "avatar_ref": row["avatar_ref"],
            "status": row["status"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_role(
        self,
        display_name: str,
        avatar_ref: str | None = None,
    ) -> dict[str, Any]:
        name, avatar = self._normalize_role_profile(display_name, avatar_ref)
        now = time.time()
        with self._conn() as c:
            for _ in range(5):
                role_id = "awr_" + secrets.token_hex(12)
                try:
                    c.execute(
                        """INSERT INTO roles(
                             role_id,display_name,avatar_ref,status,created_at,updated_at
                           ) VALUES(?,?,?,'active',?,?)""",
                        (role_id, name, avatar, now, now),
                    )
                    return self.get_role(role_id)
                except sqlite3.IntegrityError:
                    continue
        raise RoleInvalid("could not allocate a unique role_id")

    def get_role(self, role_id: str) -> dict[str, Any]:
        if not role_id:
            raise RoleNotFound("role_id is required")
        with self._conn() as c:
            row = c.execute(
                """SELECT role_id,display_name,avatar_ref,status,created_at,updated_at
                   FROM roles WHERE role_id=?""",
                (role_id,),
            ).fetchone()
        if row is None:
            raise RoleNotFound(role_id)
        return self._row_to_role(row)

    def update_role_profile(
        self,
        role_id: str,
        *,
        display_name: Any = _UNSET,
        avatar_ref: Any = _UNSET,
    ) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT role_id,display_name,avatar_ref,status,created_at,updated_at
                   FROM roles WHERE role_id=?""",
                (role_id,),
            ).fetchone()
            if row is None:
                raise RoleNotFound(role_id)
            current_name = row["display_name"]
            current_avatar = row["avatar_ref"]
            next_name = current_name if display_name is _UNSET else display_name
            next_avatar = current_avatar if avatar_ref is _UNSET else avatar_ref
            name, avatar = self._normalize_role_profile(next_name, next_avatar)
            now = time.time()
            c.execute(
                """UPDATE roles
                   SET display_name=?, avatar_ref=?, updated_at=?
                   WHERE role_id=?""",
                (name, avatar, now, role_id),
            )
        return self.get_role(role_id)

    def set_role_status(self, role_id: str, status: str) -> dict[str, Any]:
        if status not in {"active", "disabled"}:
            raise RoleInvalid("status must be active or disabled")
        with self._conn() as c:
            result = c.execute(
                "UPDATE roles SET status=?,updated_at=? WHERE role_id=?",
                (status, time.time(), role_id),
            )
        if result.rowcount != 1:
            raise RoleNotFound(role_id)
        return self.get_role(role_id)

    @staticmethod
    def _identity_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _identity_token_from_join_ticket(
        self,
        conn: sqlite3.Connection,
        ticket: str,
        ticket_id: str,
        universe: str,
        role_id: str,
    ) -> str:
        message = "\x00".join(
            ["join-identity-v1", ticket_id, universe, role_id, ticket]
        ).encode("utf-8")
        digest = hmac.new(
            self._identity_secret(conn),
            message,
            hashlib.sha256,
        ).hexdigest()
        return "awid_" + digest

    def _issue_identity_token_tx(
        self,
        conn: sqlite3.Connection,
        universe: str,
        role_id: str,
        *,
        ttl_seconds: float | None = None,
        now: float | None = None,
        token_override: str | None = None,
    ) -> dict[str, Any]:
        if not universe or not role_id:
            raise InvalidIdentityToken("universe and role_id are required")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise InvalidIdentityToken("ttl_seconds must be positive")
        issued_at = time.time() if now is None else now
        expires_at = issued_at + ttl_seconds if ttl_seconds is not None else None
        for _ in range(5):
            token_id = "awti_" + secrets.token_hex(8)
            token = (
                token_override
                if token_override is not None
                else "awid_" + secrets.token_urlsafe(32)
            )
            token_hash = self._identity_token_hash(token)
            try:
                conn.execute(
                    """INSERT INTO identity_tokens(
                         token_id,token_hash,universe,role_id,created_at,expires_at,revoked_at
                       ) VALUES(?,?,?,?,?,?,NULL)""",
                    (
                        token_id,
                        token_hash,
                        universe,
                        role_id,
                        issued_at,
                        expires_at,
                    ),
                )
                return {
                    "token_id": token_id,
                    "token": token,
                    "universe": universe,
                    "role_id": role_id,
                    "created_at": issued_at,
                    "expires_at": expires_at,
                }
            except sqlite3.IntegrityError:
                continue
        raise InvalidIdentityToken("could not allocate a unique identity token")

    def issue_identity_token(
        self,
        universe: str,
        role_id: str,
        *,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        with self._conn() as c:
            return self._issue_identity_token_tx(
                c,
                universe,
                role_id,
                ttl_seconds=ttl_seconds,
            )

    def resolve_identity_token(self, token: str) -> dict[str, Any]:
        if not token or not token.startswith("awid_"):
            raise InvalidIdentityToken("invalid identity token")
        token_hash = self._identity_token_hash(token)
        with self._conn() as c:
            row = c.execute(
                """SELECT t.token_id,t.universe,t.role_id,t.created_at,
                          t.expires_at,t.revoked_at,
                          r.status AS role_status
                   FROM identity_tokens t
                   LEFT JOIN roles r ON r.role_id=t.role_id
                   WHERE t.token_hash=?""",
                (token_hash,),
            ).fetchone()
        now = time.time()
        if row is None or row["revoked_at"] is not None:
            raise InvalidIdentityToken("identity token is invalid or revoked")
        if row["role_status"] is not None and row["role_status"] != "active":
            raise InvalidIdentityToken("role is disabled")
        if row["expires_at"] is not None and float(row["expires_at"]) <= now:
            raise InvalidIdentityToken("identity token has expired")
        return {
            "token_id": row["token_id"],
            "universe": row["universe"],
            "role_id": row["role_id"],
            "created_at": float(row["created_at"]),
            "expires_at": (
                float(row["expires_at"]) if row["expires_at"] is not None else None
            ),
        }

    def revoke_identity_token(self, token_id: str) -> bool:
        if not token_id:
            raise InvalidIdentityToken("token_id is required")
        with self._conn() as c:
            result = c.execute(
                """UPDATE identity_tokens SET revoked_at=?
                   WHERE token_id=? AND revoked_at IS NULL""",
                (time.time(), token_id),
            )
        return result.rowcount == 1

    def get_identity_token_metadata(self, token_id: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT token_id,universe,role_id,created_at,expires_at,revoked_at
                   FROM identity_tokens WHERE token_id=?""",
                (token_id,),
            ).fetchone()
        if row is None:
            raise InvalidIdentityToken("identity token not found")
        return {
            "token_id": row["token_id"],
            "universe": row["universe"],
            "role_id": row["role_id"],
            "created_at": float(row["created_at"]),
            "expires_at": (
                float(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            "revoked_at": (
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
        }

    def rotate_identity_token(
        self,
        token_id: str,
        *,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    """SELECT t.universe,t.role_id,t.revoked_at,
                              r.status AS role_status
                       FROM identity_tokens t
                       LEFT JOIN roles r ON r.role_id=t.role_id
                       WHERE t.token_id=?""",
                    (token_id,),
                ).fetchone()
                if row is None or row["revoked_at"] is not None:
                    raise InvalidIdentityToken("identity token is invalid or revoked")
                if row["role_status"] is not None and row["role_status"] != "active":
                    raise InvalidIdentityToken("role is disabled")
                replacement = self._issue_identity_token_tx(
                    c,
                    row["universe"],
                    row["role_id"],
                    ttl_seconds=ttl_seconds,
                    now=now,
                )
                c.execute(
                    "UPDATE identity_tokens SET revoked_at=? WHERE token_id=?",
                    (now, token_id),
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        replacement["rotated_from_token_id"] = token_id
        return replacement

    @staticmethod
    def _join_ticket_hash(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def issue_join_ticket(
        self,
        universe: str,
        role_id: str,
        *,
        ttl_seconds: float = 600.0,
    ) -> dict[str, Any]:
        if not universe:
            raise JoinTicketInvalid("universe is required")
        if ttl_seconds <= 0 or ttl_seconds > 86400:
            raise JoinTicketInvalid("ttl_seconds must be > 0 and <= 86400")
        role = self.get_role(role_id)
        if role["status"] != "active":
            raise RoleInactive(role_id)
        now = time.time()
        expires_at = now + ttl_seconds
        for _ in range(5):
            ticket_id = "awji_" + secrets.token_hex(8)
            ticket = "awjt_" + secrets.token_urlsafe(32)
            ticket_hash = self._join_ticket_hash(ticket)
            try:
                with self._conn() as c:
                    c.execute(
                        """INSERT INTO join_tickets(
                             ticket_id,ticket_hash,universe,role_id,
                             created_at,expires_at,used_at,used_token_id
                           ) VALUES(?,?,?,?,?,?,NULL,NULL)""",
                        (
                            ticket_id,
                            ticket_hash,
                            universe,
                            role_id,
                            now,
                            expires_at,
                        ),
                    )
                return {
                    "ticket_id": ticket_id,
                    "ticket": ticket,
                    "universe": universe,
                    "role_id": role_id,
                    "created_at": now,
                    "expires_at": expires_at,
                }
            except sqlite3.IntegrityError:
                continue
        raise JoinTicketInvalid("could not allocate a unique join ticket")

    def exchange_join_ticket(
        self,
        ticket: str,
        *,
        identity_ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not ticket or not ticket.startswith("awjt_"):
            raise JoinTicketInvalid("invalid join ticket")
        ticket_hash = self._join_ticket_hash(ticket)
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    """SELECT t.ticket_id,t.universe,t.role_id,t.expires_at,
                              t.used_at,t.used_token_id,
                              r.display_name,r.avatar_ref,r.status,
                              r.created_at AS role_created_at,
                              r.updated_at AS role_updated_at
                       FROM join_tickets t
                       JOIN roles r ON r.role_id=t.role_id
                       WHERE t.ticket_hash=?""",
                    (ticket_hash,),
                ).fetchone()
                if row is None:
                    raise JoinTicketInvalid("join ticket not found")
                if float(row["expires_at"]) <= now:
                    raise JoinTicketExpired("join ticket has expired")
                if row["status"] != "active":
                    raise RoleInactive(row["role_id"])

                derived_token = self._identity_token_from_join_ticket(
                    c,
                    ticket,
                    row["ticket_id"],
                    row["universe"],
                    row["role_id"],
                )
                replayed = row["used_at"] is not None

                if replayed:
                    token_row = c.execute(
                        """SELECT token_id,token_hash,universe,role_id,
                                  created_at,expires_at,revoked_at
                           FROM identity_tokens WHERE token_id=?""",
                        (row["used_token_id"],),
                    ).fetchone()
                    if token_row is None:
                        raise WorldRuntimeError(
                            "join ticket points to a missing identity token"
                        )
                    if token_row["revoked_at"] is not None:
                        raise JoinTicketConsumed(
                            "join ticket credential was later revoked"
                        )
                    if (
                        token_row["expires_at"] is not None
                        and float(token_row["expires_at"]) <= now
                    ):
                        raise JoinTicketConsumed(
                            "join ticket credential has expired"
                        )
                    if token_row["token_hash"] != self._identity_token_hash(
                        derived_token
                    ):
                        raise WorldRuntimeError(
                            "join ticket credential integrity mismatch"
                        )
                    identity = {
                        "token_id": token_row["token_id"],
                        "token": derived_token,
                        "universe": token_row["universe"],
                        "role_id": token_row["role_id"],
                        "created_at": float(token_row["created_at"]),
                        "expires_at": (
                            float(token_row["expires_at"])
                            if token_row["expires_at"] is not None
                            else None
                        ),
                    }
                else:
                    identity = self._issue_identity_token_tx(
                        c,
                        row["universe"],
                        row["role_id"],
                        ttl_seconds=identity_ttl_seconds,
                        now=now,
                        token_override=derived_token,
                    )
                    c.execute(
                        """UPDATE join_tickets
                           SET used_at=?,used_token_id=?
                           WHERE ticket_id=? AND used_at IS NULL""",
                        (now, identity["token_id"], row["ticket_id"]),
                    )
                    if c.execute("SELECT changes()").fetchone()[0] != 1:
                        raise JoinTicketConsumed(
                            "join ticket was concurrently consumed"
                        )

                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

        identity["join_ticket_id"] = row["ticket_id"]
        identity["replayed"] = replayed
        identity["role_profile"] = {
            "role_id": row["role_id"],
            "display_name": row["display_name"],
            "avatar_ref": row["avatar_ref"],
            "status": row["status"],
            "created_at": float(row["role_created_at"]),
            "updated_at": float(row["role_updated_at"]),
        }
        return identity

    def get_join_ticket_metadata(self, ticket_id: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT ticket_id,universe,role_id,created_at,expires_at,
                          used_at,used_token_id
                   FROM join_tickets WHERE ticket_id=?""",
                (ticket_id,),
            ).fetchone()
        if row is None:
            raise JoinTicketInvalid("join ticket not found")
        return {
            "ticket_id": row["ticket_id"],
            "universe": row["universe"],
            "role_id": row["role_id"],
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "used_at": (
                float(row["used_at"]) if row["used_at"] is not None else None
            ),
            "used_token_id": row["used_token_id"],
        }

    def get_role_entry_status(
        self,
        universe: str,
        role_id: str,
    ) -> dict[str, Any]:
        role = self.get_role(role_id)
        with self._conn() as c:
            presence = c.execute(
                """SELECT first_bootstrap_at,last_bootstrap_at,bootstrap_count
                   FROM role_world_presence
                   WHERE universe=? AND role_id=?""",
                (universe, role_id),
            ).fetchone()
            ticket = c.execute(
                """SELECT ticket_id,created_at,expires_at,used_at,used_token_id
                   FROM join_tickets
                   WHERE universe=? AND role_id=?
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (universe, role_id),
            ).fetchone()
            latest_event = int(
                c.execute(
                    """SELECT COALESCE(MAX(seq),0) FROM events
                       WHERE universe=? AND recipient_role_id=?""",
                    (universe, role_id),
                ).fetchone()[0]
            )
        return {
            "universe": universe,
            "role_profile": role,
            "join_ticket": (
                {
                    "ticket_id": ticket["ticket_id"],
                    "created_at": float(ticket["created_at"]),
                    "expires_at": float(ticket["expires_at"]),
                    "used_at": (
                        float(ticket["used_at"])
                        if ticket["used_at"] is not None
                        else None
                    ),
                    "used_token_id": ticket["used_token_id"],
                }
                if ticket is not None
                else None
            ),
            "presence": (
                {
                    "first_bootstrap_at": float(presence["first_bootstrap_at"]),
                    "last_bootstrap_at": float(presence["last_bootstrap_at"]),
                    "bootstrap_count": int(presence["bootstrap_count"]),
                }
                if presence is not None
                else None
            ),
            "latest_recipient_event_seq": latest_event,
            "identity_claimed": bool(ticket and ticket["used_at"] is not None),
            "entered_world": presence is not None,
        }

    def register_function(
        self,
        universe: str,
        function_id: str,
        version: int,
        description: str,
        input_schema: dict[str, Any],
        handler: FunctionHandler,
        *,
        access: str = "write",
        availability: str = "available",
        requires_activity_claim: bool = False,
    ) -> dict[str, Any]:
        if not universe or not function_id or version <= 0:
            raise RegistryConflict("universe, function_id and positive version are required")
        if access not in {"read", "write"}:
            raise RegistryConflict("access must be read or write")
        if access == "read" and requires_activity_claim:
            raise RegistryConflict(
                "read functions cannot require an activity claim in v0.6"
            )
        Draft202012Validator.check_schema(input_schema)
        descriptor = {
            "function_id": function_id,
            "version": version,
            "description": description,
            "input_schema": input_schema,
            "access": access,
            "availability": availability,
            "requires_activity_claim": bool(requires_activity_claim),
        }
        descriptor_hash = self._sha256(descriptor)
        now = time.time()
        with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    old = c.execute(
                        """SELECT version,descriptor_hash FROM function_registry
                           WHERE universe=? AND function_id=?""",
                        (universe, function_id),
                    ).fetchone()
                    if old:
                        old_version = int(old["version"])
                        if version < old_version:
                            raise RegistryConflict(
                                f"cannot register older version {version}; current is {old_version}"
                            )
                        if version == old_version and old["descriptor_hash"] != descriptor_hash:
                            raise RegistryConflict(
                                "descriptor changed without a version bump"
                            )
                    c.execute(
                        """INSERT INTO function_registry(
                             universe,function_id,version,description,input_schema_json,
                             access,availability,requires_activity_claim,descriptor_hash,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(universe,function_id) DO UPDATE SET
                             version=excluded.version,
                             description=excluded.description,
                             input_schema_json=excluded.input_schema_json,
                             access=excluded.access,
                             availability=excluded.availability,
                             requires_activity_claim=excluded.requires_activity_claim,
                             descriptor_hash=excluded.descriptor_hash,
                             updated_at=excluded.updated_at""",
                        (
                            universe,
                            function_id,
                            version,
                            description,
                            self._canonical(input_schema),
                            access,
                            availability,
                            1 if requires_activity_claim else 0,
                            descriptor_hash,
                            now,
                        ),
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            self._handlers[(universe, function_id, version)] = handler
        return descriptor

    def _row_to_descriptor(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "function_id": row["function_id"],
            "version": int(row["version"]),
            "description": row["description"],
            "input_schema": json.loads(row["input_schema_json"]),
            "access": row["access"],
            "availability": row["availability"],
            "requires_activity_claim": bool(row["requires_activity_claim"]),
        }

    def list_functions(self, universe: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM function_registry
                   WHERE universe=? ORDER BY function_id""",
                (universe,),
            ).fetchall()
        # A persisted descriptor without a handler in this process is not
        # callable and must never be advertised as a working world function.
        return [
            self._row_to_descriptor(r)
            for r in rows
            if (universe, r["function_id"], int(r["version"])) in self._handlers
        ]

    def get_function(self, universe: str, function_id: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM function_registry
                   WHERE universe=? AND function_id=?""",
                (universe, function_id),
            ).fetchone()
        if row is None:
            raise FunctionNotFound(f"{universe}/{function_id}")
        return self._row_to_descriptor(row)

    def start_activity(
        self,
        universe: str,
        activity_id: str,
        role_id: str,
        kind: str,
        *,
        exclusive_group: str | None = None,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        expires_at = (now + ttl_seconds) if ttl_seconds is not None else None
        with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    c.execute(
                        """UPDATE activities SET status='expired'
                           WHERE universe=? AND status='active'
                             AND expires_at IS NOT NULL AND expires_at<=?""",
                        (universe, now),
                    )
                    existing = c.execute(
                        """SELECT role_id,kind,exclusive_group,status,expires_at,created_at
                           FROM activities WHERE universe=? AND activity_id=?""",
                        (universe, activity_id),
                    ).fetchone()
                    if existing:
                        if (
                            existing["role_id"] != role_id
                            or existing["kind"] != kind
                            or existing["exclusive_group"] != exclusive_group
                        ):
                            raise ActivityConflict(
                                "activity_id already belongs to a different activity"
                            )
                        if existing["status"] != "active":
                            raise ActivityConflict(
                                "activity_id already exists but is no longer active"
                            )
                        c.execute("COMMIT")
                        return {
                            "activity_id": activity_id,
                            "universe": universe,
                            "role_id": role_id,
                            "kind": kind,
                            "exclusive_group": exclusive_group,
                            "expires_at": existing["expires_at"],
                            "replayed": True,
                        }
                    if exclusive_group:
                        row = c.execute(
                            """SELECT activity_id FROM activities
                               WHERE universe=? AND role_id=? AND exclusive_group=?
                                 AND status='active'""",
                            (universe, role_id, exclusive_group),
                        ).fetchone()
                        if row:
                            raise ActivityConflict(
                                f"exclusive activity already active: {row['activity_id']}"
                            )
                    c.execute(
                        """INSERT INTO activities(
                             universe,activity_id,role_id,kind,exclusive_group,
                             status,expires_at,created_at
                           ) VALUES(?,?,?,?,?,'active',?,?)""",
                        (
                            universe,
                            activity_id,
                            role_id,
                            kind,
                            exclusive_group,
                            expires_at,
                            now,
                        ),
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            self._changed.notify_all()
        return {
            "activity_id": activity_id,
            "universe": universe,
            "role_id": role_id,
            "kind": kind,
            "exclusive_group": exclusive_group,
            "expires_at": expires_at,
            "replayed": False,
        }

    def _expire_activity_tx(
        self, conn: sqlite3.Connection, universe: str, activity_id: str, now: float
    ) -> None:
        conn.execute(
            """UPDATE activities SET status='expired'
               WHERE universe=? AND activity_id=? AND status='active'
                 AND expires_at IS NOT NULL AND expires_at<=?""",
            (universe, activity_id, now),
        )

    def claim_activity(
        self,
        universe: str,
        activity_id: str,
        role_id: str,
        runtime_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if not runtime_id or lease_seconds <= 0:
            raise ClaimBusy("runtime_id and positive lease_seconds are required")
        now = time.time()
        with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    self._expire_activity_tx(c, universe, activity_id, now)
                    activity = c.execute(
                        """SELECT role_id,status FROM activities
                           WHERE universe=? AND activity_id=?""",
                        (universe, activity_id),
                    ).fetchone()
                    if activity is None:
                        raise ActivityNotFound(activity_id)
                    if activity["status"] != "active":
                        raise ClaimFenced("activity is no longer active")
                    if activity["role_id"] != role_id:
                        raise ClaimFenced("role does not own this activity")

                    old = c.execute(
                        """SELECT * FROM activity_claims
                           WHERE universe=? AND activity_id=?""",
                        (universe, activity_id),
                    ).fetchone()
                    if old and float(old["claim_until"]) > now:
                        if old["runtime_id"] != runtime_id:
                            raise ClaimBusy(
                                f"activity currently claimed by {old['runtime_id']}"
                            )
                        epoch = int(old["claim_epoch"])
                        token = self._claim_token(
                            c, universe, activity_id, role_id, runtime_id, epoch
                        )
                        if hashlib.sha256(token.encode()).hexdigest() != old["claim_token_hash"]:
                            raise ClaimFenced("stored claim proof is inconsistent")
                        result = {
                            "activity_id": activity_id,
                            "runtime_id": runtime_id,
                            "claim_epoch": epoch,
                            "claim_token": token,
                            "claim_until": float(old["claim_until"]),
                            "replayed": True,
                        }
                        c.execute("COMMIT")
                        return result

                    epoch = (int(old["claim_epoch"]) + 1) if old else 1
                    claim_until = now + lease_seconds
                    token = self._claim_token(
                        c, universe, activity_id, role_id, runtime_id, epoch
                    )
                    token_hash = hashlib.sha256(token.encode()).hexdigest()
                    c.execute(
                        """INSERT INTO activity_claims(
                             universe,activity_id,role_id,runtime_id,claim_epoch,
                             claim_token_hash,claim_until,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(universe,activity_id) DO UPDATE SET
                             role_id=excluded.role_id,
                             runtime_id=excluded.runtime_id,
                             claim_epoch=excluded.claim_epoch,
                             claim_token_hash=excluded.claim_token_hash,
                             claim_until=excluded.claim_until,
                             updated_at=excluded.updated_at""",
                        (
                            universe,
                            activity_id,
                            role_id,
                            runtime_id,
                            epoch,
                            token_hash,
                            claim_until,
                            now,
                        ),
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        return {
            "activity_id": activity_id,
            "runtime_id": runtime_id,
            "claim_epoch": epoch,
            "claim_token": token,
            "claim_until": claim_until,
            "replayed": False,
        }

    def _validate_claim_tx(
        self,
        conn: sqlite3.Connection,
        universe: str,
        role_id: str,
        proof: dict[str, Any],
        now: float,
    ) -> str:
        required = ("activity_id", "runtime_id", "claim_epoch", "claim_token")
        if any(proof.get(k) in (None, "") for k in required):
            raise ClaimFenced("activity claim proof is incomplete")
        activity_id = str(proof["activity_id"])
        self._expire_activity_tx(conn, universe, activity_id, now)
        activity = conn.execute(
            """SELECT role_id,status FROM activities
               WHERE universe=? AND activity_id=?""",
            (universe, activity_id),
        ).fetchone()
        if activity is None or activity["status"] != "active":
            raise ClaimFenced("activity is not active")
        if activity["role_id"] != role_id:
            raise ClaimFenced("activity belongs to another role")

        claim = conn.execute(
            """SELECT * FROM activity_claims
               WHERE universe=? AND activity_id=?""",
            (universe, activity_id),
        ).fetchone()
        if claim is None:
            raise ClaimFenced("activity has no claim")
        supplied_token_hash = hashlib.sha256(
            str(proof["claim_token"]).encode("utf-8")
        ).hexdigest()
        if (
            claim["runtime_id"] != str(proof["runtime_id"])
            or int(claim["claim_epoch"]) != int(proof["claim_epoch"])
            or claim["claim_token_hash"] != supplied_token_hash
            or float(claim["claim_until"]) <= now
        ):
            raise ClaimFenced("claim is stale, expired, or owned by another runtime")
        return activity_id

    def get_state(
        self, universe: str, scope: str, key: str, default: Any = None
    ) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT value_json,version,updated_at FROM world_state
                   WHERE universe=? AND scope=? AND state_key=?""",
                (universe, scope, key),
            ).fetchone()
        if row is None:
            return {"value": default, "version": 0, "updated_at": None}
        return {
            "value": json.loads(row["value_json"]),
            "version": int(row["version"]),
            "updated_at": float(row["updated_at"]),
        }

    def query_function(
        self,
        universe: str,
        function_id: str,
        actor_role_id: str,
        arguments: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("PRAGMA query_only=ON")
            c.execute("BEGIN")
            try:
                row = c.execute(
                    """SELECT * FROM function_registry
                       WHERE universe=? AND function_id=?""",
                    (universe, function_id),
                ).fetchone()
                if row is None:
                    raise FunctionNotFound(f"{universe}/{function_id}")
                descriptor = self._row_to_descriptor(row)
                if descriptor["access"] != "read":
                    raise WorldRuntimeError(
                        f"function is not read-only: {function_id}"
                    )
                version = descriptor["version"]
                if expected_version is not None and expected_version != version:
                    raise FunctionVersionMismatch(
                        f"expected version {expected_version}, current is {version}"
                    )
                if descriptor["availability"] != "available":
                    raise FunctionNotFound(
                        f"function is not available: {function_id}"
                    )
                try:
                    Draft202012Validator(
                        descriptor["input_schema"]
                    ).validate(arguments)
                except ValidationError as exc:
                    path = ".".join(str(x) for x in exc.absolute_path)
                    location = f" at {path}" if path else ""
                    raise SchemaRejected(
                        f"{exc.message}{location}"
                    ) from exc

                handler = self._handlers.get((universe, function_id, version))
                if handler is None:
                    raise FunctionNotFound(
                        f"handler unavailable for {function_id}@{version}"
                    )
                ctx = FunctionContext(
                    c,
                    universe,
                    actor_role_id,
                    function_id,
                    version,
                    access="read",
                )
                try:
                    outcome = handler(ctx, arguments)
                except sqlite3.OperationalError as exc:
                    message = str(exc).lower()
                    if "readonly" in message or "query_only" in message:
                        raise WorldRuntimeError(
                            "read function attempted a database write"
                        ) from exc
                    raise
                if not isinstance(outcome, FunctionOutcome):
                    raise WorldRuntimeError(
                        "function handler must return FunctionOutcome"
                    )
                if outcome.events:
                    raise WorldRuntimeError(
                        "read function cannot emit durable events"
                    )
                json.dumps(outcome.result, ensure_ascii=False)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return {
            "ok": True,
            "function_id": function_id,
            "function_version": version,
            "result": outcome.result,
            "read_only": True,
        }

    def invoke_function(
        self,
        universe: str,
        function_id: str,
        actor_role_id: str,
        arguments: dict[str, Any],
        *,
        operation_id: str,
        expected_version: int | None = None,
        activity_claim: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not operation_id:
            raise OperationConflict("operation_id is required for write functions")
        args_hash = self._sha256({"function_id": function_id, "arguments": arguments})
        now = time.time()
        with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    old = c.execute(
                        """SELECT * FROM operations
                           WHERE universe=? AND actor_role_id=? AND idempotency_key=?""",
                        (universe, actor_role_id, operation_id),
                    ).fetchone()
                    if old:
                        if (
                            old["function_id"] != function_id
                            or old["args_hash"] != args_hash
                        ):
                            raise OperationConflict(
                                "idempotency key reused for a different operation"
                            )
                        receipt = json.loads(old["receipt_json"])
                        receipt["replayed"] = True
                        c.execute("COMMIT")
                        return receipt

                    row = c.execute(
                        """SELECT * FROM function_registry
                           WHERE universe=? AND function_id=?""",
                        (universe, function_id),
                    ).fetchone()
                    if row is None:
                        raise FunctionNotFound(f"{universe}/{function_id}")
                    descriptor = self._row_to_descriptor(row)
                    version = descriptor["version"]
                    if expected_version is not None and expected_version != version:
                        raise FunctionVersionMismatch(
                            f"expected version {expected_version}, current is {version}"
                        )
                    if descriptor["availability"] != "available":
                        raise FunctionNotFound(
                            f"function is not available: {function_id}"
                        )
                    try:
                        Draft202012Validator(descriptor["input_schema"]).validate(
                            arguments
                        )
                    except ValidationError as exc:
                        path = ".".join(str(x) for x in exc.absolute_path)
                        location = f" at {path}" if path else ""
                        raise SchemaRejected(f"{exc.message}{location}") from exc

                    activity_id = None
                    if descriptor["requires_activity_claim"]:
                        if not activity_claim:
                            raise ClaimFenced(
                                "this function requires an activity claim"
                            )
                        activity_id = self._validate_claim_tx(
                            c, universe, actor_role_id, activity_claim, now
                        )

                    handler = self._handlers.get((universe, function_id, version))
                    if handler is None:
                        raise FunctionNotFound(
                            f"handler unavailable for {function_id}@{version}"
                        )

                    ctx = FunctionContext(
                        c,
                        universe,
                        actor_role_id,
                        function_id,
                        version,
                        activity_id,
                    )
                    outcome = handler(ctx, arguments)
                    if not isinstance(outcome, FunctionOutcome):
                        raise WorldRuntimeError(
                            "function handler must return FunctionOutcome"
                        )
                    json.dumps(outcome.result, ensure_ascii=False)

                    event_seqs: list[int] = []
                    for event in outcome.events:
                        cur = c.execute(
                            """INSERT INTO events(
                                 universe,recipient_role_id,actor_role_id,kind,
                                 payload_json,created_at
                               ) VALUES(?,?,?,?,?,?)""",
                            (
                                universe,
                                event.recipient_role_id,
                                actor_role_id,
                                event.kind,
                                self._canonical(event.payload),
                                now,
                            ),
                        )
                        event_seqs.append(int(cur.lastrowid))

                    receipt = {
                        "ok": True,
                        "operation_id": operation_id,
                        "function_id": function_id,
                        "function_version": version,
                        "result": outcome.result,
                        "event_seqs": event_seqs,
                        "replayed": False,
                    }
                    c.execute(
                        """INSERT INTO operations(
                             universe,actor_role_id,idempotency_key,function_id,
                             function_version,args_hash,receipt_json,created_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            universe,
                            actor_role_id,
                            operation_id,
                            function_id,
                            version,
                            args_hash,
                            self._canonical(receipt),
                            now,
                        ),
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            self._changed.notify_all()
            return receipt

    def read_changes(
        self,
        universe: str,
        role_id: str,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        floor = self.event_floor(universe)
        if after_seq < floor:
            raise CursorExpired(
                f"cursor {after_seq} is older than retained floor {floor}"
            )
        with self._conn() as c:
            rows = c.execute(
                """SELECT seq,universe,recipient_role_id,actor_role_id,kind,
                          payload_json,created_at
                   FROM events
                   WHERE universe=? AND recipient_role_id=? AND seq>?
                   ORDER BY seq LIMIT ?""",
                (universe, role_id, after_seq, limit),
            ).fetchall()
        return [
            {
                "seq": int(r["seq"]),
                "universe": r["universe"],
                "recipient_role_id": r["recipient_role_id"],
                "actor_role_id": r["actor_role_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "created_at": float(r["created_at"]),
            }
            for r in rows
        ]

    def wait_changes(
        self,
        universe: str,
        role_id: str,
        after_seq: int = 0,
        timeout: float = 5.0,
        limit: int = 50,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._changed:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return []
                rows = self.read_changes(universe, role_id, after_seq, limit)
                if rows:
                    return rows
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._changed.wait(remaining)

    def wake_waiters(self) -> None:
        # Used by adapters to stop a cancelled bounded wait promptly.
        with self._changed:
            self._changed.notify_all()

    def event_floor(self, universe: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT floor FROM event_floors WHERE universe=?", (universe,)
            ).fetchone()
        return int(row["floor"]) if row else 0

    def cleanup_events(self, universe: str, through_seq: int) -> int:
        with self._lock:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    row = c.execute(
                        """SELECT MAX(seq) AS max_seq FROM events
                           WHERE universe=? AND seq<=?""",
                        (universe, through_seq),
                    ).fetchone()
                    candidate = (
                        int(row["max_seq"])
                        if row and row["max_seq"] is not None
                        else None
                    )
                    deleted = c.execute(
                        "DELETE FROM events WHERE universe=? AND seq<=?",
                        (universe, through_seq),
                    ).rowcount
                    old = c.execute(
                        "SELECT floor FROM event_floors WHERE universe=?",
                        (universe,),
                    ).fetchone()
                    floor = int(old["floor"]) if old else 0
                    if candidate is not None:
                        floor = max(floor, candidate)
                    c.execute(
                        """INSERT INTO event_floors(universe,floor) VALUES(?,?)
                           ON CONFLICT(universe)
                           DO UPDATE SET floor=excluded.floor""",
                        (universe, floor),
                    )
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            self._changed.notify_all()
        return int(deleted)

    def bootstrap(self, universe: str, role_id: str) -> dict[str, Any]:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO role_world_presence(
                     universe,role_id,first_bootstrap_at,last_bootstrap_at,bootstrap_count
                   ) VALUES(?,?,?,?,1)
                   ON CONFLICT(universe,role_id) DO UPDATE SET
                     last_bootstrap_at=excluded.last_bootstrap_at,
                     bootstrap_count=role_world_presence.bootstrap_count+1""",
                (universe, role_id, now, now),
            )
            # Bootstrap must not advertise time-expired activities as active
            # merely because no separate cleanup sweep has run yet.
            c.execute(
                """UPDATE activities SET status='expired'
                   WHERE universe=? AND status='active'
                     AND expires_at IS NOT NULL AND expires_at<=?""",
                (universe, now),
            )
            latest = int(
                c.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=?",
                    (universe,),
                ).fetchone()[0]
            )
            activities = [
                dict(row)
                for row in c.execute(
                    """SELECT universe,activity_id,role_id,kind,exclusive_group,
                              status,expires_at,created_at
                       FROM activities
                       WHERE universe=? AND role_id=? AND status='active'
                       ORDER BY activity_id""",
                    (universe, role_id),
                ).fetchall()
            ]
            role_row = c.execute(
                """SELECT role_id,display_name,avatar_ref,status,created_at,updated_at
                   FROM roles WHERE role_id=?""",
                (role_id,),
            ).fetchone()
        floor = self.event_floor(universe)
        latest_cursor = max(latest, floor)
        role_profile = self._row_to_role(role_row) if role_row is not None else None
        return {
            "universe": universe,
            "role_id": role_id,
            "role_profile": role_profile,
            "functions": self.list_functions(universe),
            "active_activities": activities,
            "event_floor": floor,
            "latest_event_seq": latest_cursor,
            "world_entry_state": {
                "active_activities": activities,
                "event_floor": floor,
                "latest_event_seq": latest_cursor,
            },
        }
