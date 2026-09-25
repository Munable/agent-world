from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from .runtime_contracts import (
    identifier,
    duration,
    json_text,
    PROTOCOL_VERSION,
)
from pathlib import Path
from typing import Any, Callable


from .runtime_errors import (
    WorldRuntimeError,
    RegistryConflict,
    FunctionNotFound,
    FunctionVersionMismatch,
    SchemaRejected,
    OperationConflict,
    ActivityConflict,
    ActivityNotFound,
    ClaimBusy,
    ClaimFenced,
    CursorExpired,
    AuthenticationRequired,
    InvalidIdentityToken,
    IdentityScopeMismatch,
    RoleNotFound,
    RoleInvalid,
    RoleInactive,
    JoinTicketInvalid,
    JoinTicketExpired,
    JoinTicketConsumed,
    InvalidArguments,
    AccessModeMismatch,
    PermissionDenied,
    ReceiptNotFound,
    StateConflict,
    ResultRejected,
    WorldDefinitionError,
    WorldVersionMismatch,
    CursorInvalid,
    StorageBusy,
    RuleViolation,
)

_UNSET = object()


from .world_types import EventSpec, FunctionOutcome

from .world_context import FunctionContext


FunctionHandler = Callable[[FunctionContext, dict[str, Any]], FunctionOutcome]


from .runtime_functions import RuntimeFunctions


from .runtime_events import RuntimeEvents


from .runtime_activities import RuntimeActivities


from .runtime_journal import RuntimeJournal
from .runtime_views import RuntimeViews
from .runtime_timers import RuntimeTimers
from .retention import RuntimeRetention
from .runtime_streams import RuntimeStreams


class WorldRuntime(RuntimeFunctions, RuntimeEvents, RuntimeActivities, RuntimeJournal, RuntimeViews, RuntimeTimers, RuntimeRetention, RuntimeStreams):
    def __init__(self, db_path: str | Path):
        if str(db_path) == ":memory:":
            raise ValueError("WorldRuntime requires a file-backed SQLite database")
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._worlds: dict[str, Any] = {}
        self._function_options: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._handlers: dict[tuple[str, str, int], FunctionHandler] = {}
        self._init_db()

    @contextmanager
    def _conn(self, *, readonly: bool = False):
        location = Path(self.db_path).as_uri() + "?mode=ro" if readonly else self.db_path
        conn = sqlite3.connect(location, timeout=5, isolation_level=None, uri=readonly, cached_statements=0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _enable_wal(c):
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if c.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    c.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    @staticmethod
    def _schema_script(c, script):
        c.execute("BEGIN IMMEDIATE")
        if c.execute("PRAGMA user_version").fetchone()[0] > 5:
            raise WorldVersionMismatch("database schema is newer than this runtime")
        for statement in script.split(";"):
            statement = statement.strip()
            if statement and statement.upper() != "BEGIN IMMEDIATE":
                c.execute(statement)

    def _init_db(self) -> None:
        with self._conn() as c:
            if c.execute("PRAGMA user_version").fetchone()[0] > 5:
                raise WorldVersionMismatch("database schema is newer than this runtime")
            self._enable_wal(c)
            self._schema_script(
                c,
                """
                BEGIN IMMEDIATE;
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
                """,
            )
            row = c.execute("SELECT meta_value FROM runtime_meta WHERE meta_key='claim_secret'").fetchone()
            if row is None:
                c.execute(
                    "INSERT OR IGNORE INTO runtime_meta(meta_key,meta_value) VALUES('claim_secret',?)",
                    (secrets.token_hex(32),),
                )
            row = c.execute("SELECT meta_value FROM runtime_meta WHERE meta_key='identity_secret'").fetchone()
            if row is None:
                c.execute(
                    "INSERT OR IGNORE INTO runtime_meta(meta_key,meta_value) VALUES('identity_secret',?)",
                    (secrets.token_hex(32),),
                )

            for table, column, declaration in (
                ("function_registry", "output_schema_json", "TEXT"),
                ("operations", "activity_id", "TEXT"),
                ("world_state", "deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("join_tickets", "revoked_at", "REAL"),
                ("activities", "requested_ttl", "REAL"),
            ):
                names = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
                if column not in names:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            c.execute("""CREATE TABLE IF NOT EXISTS world_definitions(
                universe TEXT PRIMARY KEY, world_id TEXT NOT NULL,
                version INTEGER NOT NULL, manifest_json TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 1)""")
            if "state_version" not in {r["name"] for r in c.execute("PRAGMA table_info(world_definitions)")}:
                c.execute("ALTER TABLE world_definitions ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_recipient ON events(universe,recipient_role_id,seq)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_actor ON operations(universe,actor_role_id,created_at)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_active_roles ON activities(universe,role_id,status)")
            c.execute(
                "UPDATE activities SET requested_ttl=expires_at-created_at WHERE expires_at IS NOT NULL AND requested_ttl IS NULL"
            )
            self._init_journal_tx(c)
            self._init_views_tx(c)
            self._init_timers_tx(c)
            if "access_mode" not in {r["name"] for r in c.execute("PRAGMA table_info(identity_tokens)")}:
                c.execute("ALTER TABLE identity_tokens ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'control'")
            self._init_streams_tx(c)
            c.execute("PRAGMA user_version=5")
            c.execute("COMMIT")

    @staticmethod
    def _canonical(value: Any) -> str:
        return json_text(value)

    @classmethod
    def _sha256(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical(value).encode("utf-8")).hexdigest()

    def _claim_secret(self, conn: sqlite3.Connection) -> bytes:
        row = conn.execute("SELECT meta_value FROM runtime_meta WHERE meta_key='claim_secret'").fetchone()
        if row is None:
            raise WorldRuntimeError("claim secret missing")
        return bytes.fromhex(row["meta_value"])

    def _identity_secret(self, conn: sqlite3.Connection) -> bytes:
        row = conn.execute("SELECT meta_value FROM runtime_meta WHERE meta_key='identity_secret'").fetchone()
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
        message = "\x00".join(["claim-v1", universe, activity_id, role_id, runtime_id, str(epoch)]).encode(
            "utf-8"
        )
        digest = hmac.new(self._claim_secret(conn), message, hashlib.sha256).hexdigest()
        return "awc_" + digest

    @staticmethod
    def _normalize_role_profile(display_name: str, avatar_ref: str | None):
        if not isinstance(display_name, str):
            raise RoleInvalid("display_name must be a string")
        name = display_name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise RoleInvalid("display_name must be 1..80 printable characters")
        if avatar_ref is not None and not isinstance(avatar_ref, str):
            raise RoleInvalid("avatar_ref must be a string or null")
        avatar = avatar_ref.strip() or None if avatar_ref is not None else None
        if avatar is not None and (len(avatar) > 1024 or any(ord(c) < 32 for c in avatar)):
            raise RoleInvalid("invalid avatar_ref")
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

    def update_role_profile(self, role_id, *, display_name=_UNSET, avatar_ref=_UNSET):
        identifier(role_id, "role_id")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone()
            if row is None:
                raise RoleNotFound(role_id)
            name, avatar = self._normalize_role_profile(
                row["display_name"] if display_name is _UNSET else display_name,
                row["avatar_ref"] if avatar_ref is _UNSET else avatar_ref,
            )
            c.execute(
                "UPDATE roles SET display_name=?,avatar_ref=?,updated_at=? WHERE role_id=?",
                (name, avatar, time.time(), role_id),
            )
            result = self._row_to_role(
                c.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone()
            )
        return result

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
        message = "\x00".join(["join-identity-v1", ticket_id, universe, role_id, ticket]).encode("utf-8")
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
        access_mode: str = "control",
    ) -> dict[str, Any]:
        identifier(universe, "universe")
        identifier(role_id, "role_id")
        if ttl_seconds is not None:
            ttl_seconds = duration(ttl_seconds, "identity ttl_seconds", 315360000)
        if not isinstance(access_mode, str) or access_mode not in {"control", "observe"}:
            raise InvalidArguments("identity access_mode must be control or observe")
        issued_at = time.time() if now is None else now
        expires_at = issued_at + ttl_seconds if ttl_seconds is not None else None
        for _ in range(5):
            token_id = "awti_" + secrets.token_hex(8)
            token = token_override if token_override is not None else "awid_" + secrets.token_urlsafe(32)
            token_hash = self._identity_token_hash(token)
            try:
                conn.execute(
                    """INSERT INTO identity_tokens(
                         token_id,token_hash,universe,role_id,created_at,expires_at,revoked_at,access_mode
                       ) VALUES(?,?,?,?,?,?,NULL,?)""",
                    (
                        token_id,
                        token_hash,
                        universe,
                        role_id,
                        issued_at,
                        expires_at,
                        access_mode,
                    ),
                )
                return {
                    "token_id": token_id,
                    "token": token,
                    "universe": universe,
                    "role_id": role_id,
                    "created_at": issued_at,
                    "expires_at": expires_at,
                    "access_mode": access_mode,
                }
            except sqlite3.IntegrityError:
                continue
        raise InvalidIdentityToken("could not allocate a unique identity token")

    def issue_identity_token(self, universe, role_id, *, ttl_seconds=None, access_mode="control"):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_actor_tx(c, universe, role_id)
            return self._issue_identity_token_tx(c, universe, role_id, ttl_seconds=ttl_seconds, access_mode=access_mode)

    def _identity_metadata_tx(self, c, token, *, universe=None, role_id=None):
        if not isinstance(token, str) or not token.startswith("awid_") or len(token) > 256:
            raise InvalidIdentityToken("invalid identity token")
        row = c.execute(
            "SELECT t.*,r.status AS role_status FROM identity_tokens t "
            "LEFT JOIN roles r ON r.role_id=t.role_id WHERE t.token_hash=?",
            (self._identity_token_hash(token),),
        ).fetchone()
        now = time.time()
        if row is None or row["revoked_at"] is not None:
            raise InvalidIdentityToken("identity token is invalid or revoked")
        if row["expires_at"] is not None and float(row["expires_at"]) <= now:
            raise InvalidIdentityToken("identity token has expired")
        if row["role_status"] not in (None, "active"):
            raise InvalidIdentityToken("role is disabled")
        if universe is not None and row["universe"] != universe:
            raise IdentityScopeMismatch("identity token belongs to another universe")
        if role_id is not None and row["role_id"] != role_id:
            raise IdentityScopeMismatch("identity token belongs to another role")
        return {key: row[key] for key in ("token_id", "universe", "role_id", "created_at", "expires_at", "access_mode")}

    def _admit_actor_tx(self, c, universe, role_id, identity_token=None, *, require_control=False):
        identifier(universe, "universe")
        identifier(role_id, "role_id")
        if identity_token is not None:
            identity = self._identity_metadata_tx(c, identity_token, universe=universe, role_id=role_id)
            if require_control and identity["access_mode"] != "control":
                raise PermissionDenied("observation credential cannot control this role")
            return identity
        # Omitting a token is only for trusted in-process code and explicit test mode.
        row = c.execute("SELECT status FROM roles WHERE role_id=?", (role_id,)).fetchone()
        if row is not None and row["status"] != "active":
            raise RoleInactive("role is disabled")
        return None

    def resolve_identity_token(self, token: str) -> dict[str, Any]:
        with self._conn(readonly=True) as c:
            return self._identity_metadata_tx(c, token)

    def revoke_identity_token(self, token_id: str, *, expected_universe=None) -> bool:
        if not token_id:
            raise InvalidIdentityToken("token_id is required")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT universe FROM identity_tokens WHERE token_id=?", (token_id,)).fetchone()
            if row is not None and expected_universe is not None and row["universe"] != expected_universe:
                raise IdentityScopeMismatch("identity token belongs to another universe")
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
            "expires_at": (float(row["expires_at"]) if row["expires_at"] is not None else None),
            "revoked_at": (float(row["revoked_at"]) if row["revoked_at"] is not None else None),
        }

    def rotate_identity_token(
        self,
        token_id: str,
        *,
        ttl_seconds: float | None = None,
        expected_universe: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    """SELECT t.universe,t.role_id,t.revoked_at,t.access_mode,
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
                if expected_universe is not None and row["universe"] != expected_universe:
                    raise IdentityScopeMismatch("identity token belongs to another universe")
                now = time.time()
                replacement = self._issue_identity_token_tx(
                    c,
                    row["universe"],
                    row["role_id"],
                    ttl_seconds=ttl_seconds,
                    now=now,
                    access_mode=row["access_mode"],
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

    def issue_join_ticket(self, universe, role_id, *, ttl_seconds=600.0):
        identifier(universe, "universe")
        identifier(role_id, "role_id")
        ttl_seconds = duration(ttl_seconds, "join ttl_seconds")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            role = c.execute("SELECT status FROM roles WHERE role_id=?", (role_id,)).fetchone()
            if role is None:
                raise RoleNotFound(role_id)
            if role["status"] != "active":
                raise RoleInactive(role_id)
            now = time.time()
            ticket_id = "awji_" + secrets.token_hex(16)
            ticket = "awjt_" + secrets.token_urlsafe(32)
            c.execute(
                "INSERT INTO join_tickets(ticket_id,ticket_hash,universe,role_id,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (ticket_id, self._join_ticket_hash(ticket), universe, role_id, now, now + ttl_seconds),
            )
            return {
                "ticket_id": ticket_id,
                "ticket": ticket,
                "universe": universe,
                "role_id": role_id,
                "created_at": now,
                "expires_at": now + ttl_seconds,
            }

    def exchange_join_ticket(
        self,
        ticket: str,
        *,
        identity_ttl_seconds: float | None = None,
        expected_universe: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(ticket, str) or len(ticket) > 256 or not ticket.startswith("awjt_"):
            raise JoinTicketInvalid("invalid join ticket")
        ticket_hash = self._join_ticket_hash(ticket)
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    """SELECT t.ticket_id,t.universe,t.role_id,t.expires_at,
                              t.used_at,t.used_token_id,t.revoked_at,
                              r.display_name,r.avatar_ref,r.status,
                              r.created_at AS role_created_at,
                              r.updated_at AS role_updated_at
                       FROM join_tickets t
                       JOIN roles r ON r.role_id=t.role_id
                       WHERE t.ticket_hash=?""",
                    (ticket_hash,),
                ).fetchone()
                now = time.time()
                if row is None or row["revoked_at"] is not None:
                    raise JoinTicketInvalid("join ticket is invalid or revoked")
                if expected_universe is not None and row["universe"] != expected_universe:
                    raise IdentityScopeMismatch("join ticket belongs to another universe")
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
                        raise WorldRuntimeError("join ticket points to a missing identity token")
                    if token_row["revoked_at"] is not None:
                        raise JoinTicketConsumed("join ticket credential was later revoked")
                    if token_row["expires_at"] is not None and float(token_row["expires_at"]) <= now:
                        raise JoinTicketConsumed("join ticket credential has expired")
                    if token_row["token_hash"] != self._identity_token_hash(derived_token):
                        raise WorldRuntimeError("join ticket credential integrity mismatch")
                    identity = {
                        "token_id": token_row["token_id"],
                        "token": derived_token,
                        "universe": token_row["universe"],
                        "role_id": token_row["role_id"],
                        "created_at": float(token_row["created_at"]),
                        "expires_at": (
                            float(token_row["expires_at"]) if token_row["expires_at"] is not None else None
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
                        raise JoinTicketConsumed("join ticket was concurrently consumed")

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

    def revoke_join_ticket(self, ticket_id, *, expected_universe=None):
        identifier(ticket_id, "ticket_id")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT universe FROM join_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            if row is None:
                return False
            if expected_universe is not None and row["universe"] != expected_universe:
                raise IdentityScopeMismatch("join ticket belongs to another universe")
            return (
                c.execute(
                    "UPDATE join_tickets SET revoked_at=? WHERE ticket_id=? AND revoked_at IS NULL",
                    (time.time(), ticket_id),
                ).rowcount
                == 1
            )

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
            "used_at": (float(row["used_at"]) if row["used_at"] is not None else None),
            "used_token_id": row["used_token_id"],
        }

    def get_role_entry_status(self, universe, role_id):
        identifier(universe, "universe")
        identifier(role_id, "role_id")
        with self._conn(readonly=True) as c:
            c.execute("BEGIN")
            role = c.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone()
            if role is None:
                raise RoleNotFound(role_id)
            presence = c.execute(
                "SELECT first_bootstrap_at,last_bootstrap_at,bootstrap_count FROM role_world_presence WHERE universe=? AND role_id=?",
                (universe, role_id),
            ).fetchone()
            ticket = c.execute(
                "SELECT ticket_id,created_at,expires_at,used_at,used_token_id,revoked_at FROM join_tickets "
                "WHERE universe=? AND role_id=? ORDER BY created_at DESC,ticket_id DESC LIMIT 1",
                (universe, role_id),
            ).fetchone()
            claimed = c.execute(
                "SELECT MIN(used_at) FROM join_tickets WHERE universe=? AND role_id=? AND used_at IS NOT NULL",
                (universe, role_id),
            ).fetchone()[0]
            actions = c.execute(
                "SELECT COUNT(*),MAX(created_at) FROM operations WHERE universe=? AND actor_role_id=?",
                (universe, role_id),
            ).fetchone()
            latest = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=? AND recipient_role_id=?",
                (universe, role_id),
            ).fetchone()[0]
            active_credentials = c.execute(
                "SELECT COUNT(*) FROM identity_tokens WHERE universe=? AND role_id=? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>?)",
                (universe, role_id, time.time()),
            ).fetchone()[0]
        return {
            "universe": universe,
            "role_profile": self._row_to_role(role),
            "join_ticket": dict(ticket) if ticket else None,
            "presence": dict(presence) if presence else None,
            "latest_recipient_event_seq": int(latest),
            "identity_claimed": claimed is not None,
            "identity_claimed_at": claimed,
            "has_active_credential": bool(active_credentials) and role["status"] == "active",
            "world_action_count": int(actions[0]),
            "last_world_action_at": actions[1],
            "entered_world": presence is not None,
        }

    def get_state(self, universe, scope, key, default=None):
        identifier(universe, "universe")
        with self._conn(readonly=True) as c:
            return FunctionContext(
                c, universe, "system:inspector", "state.inspect", 1, access="read"
            ).get_state_record(scope, key, default)

    def install_world(self, universe, definition):
        from .world_sdk import install_world

        return install_world(self, universe, definition)

    def get_world_manifest(self, universe):
        identifier(universe, "universe")
        with self._conn(readonly=True) as c:
            row = c.execute(
                "SELECT manifest_json FROM world_definitions WHERE universe=?", (universe,)
            ).fetchone()
        return json.loads(row["manifest_json"]) if row else None

    def describe_world(self, universe, role_id, *, identity_token=None):
        """Return the installed world's stable entry contract for an authorized role."""
        identifier(universe, "universe")
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            row = c.execute(
                "SELECT manifest_json FROM world_definitions WHERE universe=?", (universe,)
            ).fetchone()
            if row is None:
                world = {"id": universe, "name": universe, "version": None, "state_version": None}
                instructions = "Discover the world functions, then inspect the current state before acting."
            else:
                manifest = json.loads(row["manifest_json"])
                world = {
                    "id": manifest["world_id"],
                    "name": manifest["display_name"],
                    "version": manifest["version"],
                    "state_version": manifest.get("state_version"),
                    "api_version": manifest.get("api_version"),
                }
                instructions = manifest.get(
                    "entry_instructions",
                    "Discover the world functions, then inspect the current state before acting.",
                )
            result = {
                "world": world,
                "entry_instructions": instructions,
                "discovery": {"tool": "world.discover", "include_schemas": True},
            }
            json_text(result)
            return result

    def bootstrap(
        self, universe, role_id, *, identity_token=None, include_catalog=False, record_presence=False
    ):
        if type(include_catalog) is not bool or type(record_presence) is not bool:
            raise InvalidArguments("bootstrap flags must be booleans")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            identity = self._admit_actor_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            if record_presence and (identity is None or identity["access_mode"] == "control"):
                c.execute(
                    "INSERT INTO role_world_presence(universe,role_id,first_bootstrap_at,last_bootstrap_at,bootstrap_count) "
                    "VALUES(?,?,?,?,1) ON CONFLICT(universe,role_id) DO UPDATE SET "
                    "last_bootstrap_at=excluded.last_bootstrap_at,bootstrap_count=role_world_presence.bootstrap_count+1",
                    (universe, role_id, now, now),
                )
            if identity is None or identity["access_mode"] == "control":
                c.execute(
                    "UPDATE activities SET status='expired' WHERE universe=? AND status='active' "
                    "AND expires_at IS NOT NULL AND expires_at<=?",
                    (universe, now),
                )
            floor_row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
            floor = int(floor_row["floor"]) if floor_row else 0
            latest = max(
                floor,
                int(
                    c.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=?", (universe,)
                    ).fetchone()[0]
                ),
            )
            rows = c.execute(
                "SELECT universe,activity_id,role_id,kind,exclusive_group,status,expires_at,created_at "
                "FROM activities WHERE universe=? AND role_id=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY activity_id LIMIT 51",
                (universe, role_id, now),
            ).fetchall()
            profile = c.execute("SELECT * FROM roles WHERE role_id=?", (role_id,)).fetchone()
            definition = self._worlds.get(universe)
            entry = {"latest_event_seq": latest}
            if definition is not None and definition.bootstrap is not None:
                ctx = self._context_tx(c, universe, role_id, "world.bootstrap", 1, now=now)
                view = self._run_user_code(c, definition.bootstrap, ctx, read_only=True)
                if not isinstance(view, dict):
                    raise ResultRejected("world bootstrap must return an object")
                json_text(view, maximum=65536)
                entry["view"] = view
            self._admit_actor_tx(c, universe, role_id, identity_token)
            result = {
                "protocol_version": PROTOCOL_VERSION,
                "universe": universe,
                "role_id": role_id,
                "role_profile": self._row_to_role(profile) if profile else None,
                "world": {
                    "id": definition.world_id if definition else universe,
                    "name": definition.display_name if definition else universe,
                    "version": definition.version if definition else None,
                },
                "active_activities": [dict(r) for r in rows[:50]],
                "activities_has_more": len(rows) > 50,
                "event_floor": floor,
                "latest_event_seq": latest,
                "snapshot_cursor": latest,
                "world_entry_state": entry,
                "world_guide": {"tool": "world.describe"},
                "discovery": {"tool": "world.discover", "include_schemas": True},
            }
            if include_catalog:
                catalog = self._catalog_tx(c, universe, role_id)
                result["functions"] = [d for d in catalog if d["access"] == "read"] if identity and identity["access_mode"] == "observe" else catalog
            json_text(result)
        return result


__all__ = [
    "AccessModeMismatch",
    "ActivityConflict",
    "ActivityNotFound",
    "AuthenticationRequired",
    "ClaimBusy",
    "ClaimFenced",
    "CursorExpired",
    "CursorInvalid",
    "EventSpec",
    "FunctionContext",
    "FunctionNotFound",
    "FunctionOutcome",
    "FunctionVersionMismatch",
    "IdentityScopeMismatch",
    "InvalidArguments",
    "InvalidIdentityToken",
    "JoinTicketConsumed",
    "JoinTicketExpired",
    "JoinTicketInvalid",
    "OperationConflict",
    "PermissionDenied",
    "ReceiptNotFound",
    "RegistryConflict",
    "ResultRejected",
    "RoleInactive",
    "RoleInvalid",
    "RoleNotFound",
    "RuleViolation",
    "SchemaRejected",
    "StateConflict",
    "StorageBusy",
    "WorldDefinitionError",
    "WorldRuntime",
    "WorldRuntimeError",
    "WorldVersionMismatch",
]
