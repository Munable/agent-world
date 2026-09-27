from __future__ import annotations

"""Cross-world identity proof experiment.

This module is deliberately outside the agent_world package. It tests one candidate
architecture with two independent Runtime deployments; it is not a stable protocol.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_world import WorldRuntime
from agent_world.errors import InvalidIdentityToken


AUTH_CAPABILITY = "identity.authenticate"
DELEGATION_VERSION = "aw-delegation-v1"
PROOF_VERSION = "aw-identity-proof-v1"
CHALLENGE_VERSION = "aw-identity-challenge-v1"
REVOCATION_VERSION = "aw-delegation-revocation-v1"
ROTATION_VERSION = "aw-root-rotation-v1"


class IdentityExperimentError(ValueError):
    pass


class ChallengeRejected(IdentityExperimentError):
    pass


class DelegationRejected(IdentityExperimentError):
    pass


class ProofRejected(IdentityExperimentError):
    pass


class RootRotationRejected(IdentityExperimentError):
    pass


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64d(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise IdentityExperimentError(f"invalid {label}")
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise IdentityExperimentError(f"invalid {label}") from exc


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _public_key_text(key: Ed25519PublicKey) -> str:
    return _b64e(_public_bytes(key))


def root_id_from_public_key(public_key_text: str) -> str:
    raw = _b64d(public_key_text, "root public key")
    if len(raw) != 32:
        raise IdentityExperimentError("invalid root public key length")
    return "awuid_" + hashlib.sha256(raw).hexdigest()


def _load_public_key(value: str, label: str) -> Ed25519PublicKey:
    raw = _b64d(value, label)
    if len(raw) != 32:
        raise IdentityExperimentError(f"invalid {label} length")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise IdentityExperimentError(f"invalid {label}") from exc


def _verify(public_key_text: str, signature_text: str, payload: dict[str, Any], label: str) -> None:
    key = _load_public_key(public_key_text, label + " public key")
    signature = _b64d(signature_text, label + " signature")
    if len(signature) != 64:
        raise IdentityExperimentError(f"invalid {label} signature length")
    try:
        key.verify(signature, _canonical(payload))
    except InvalidSignature as exc:
        raise IdentityExperimentError(f"invalid {label} signature") from exc


def _identifier(value: str, label: str, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise IdentityExperimentError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class Ed25519Identity:
    _private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "Ed25519Identity":
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> str:
        return _public_key_text(self._private_key.public_key())

    @property
    def root_id(self) -> str:
        return root_id_from_public_key(self.public_key)

    def sign(self, payload: dict[str, Any]) -> str:
        return _b64e(self._private_key.sign(_canonical(payload)))


def create_delegation(
    root: Ed25519Identity,
    delegate: Ed25519Identity,
    *,
    delegation_id: str,
    audiences: list[str],
    issued_at: float,
    expires_at: float,
) -> dict[str, Any]:
    _identifier(delegation_id, "delegation_id")
    if not audiences or len(audiences) > 16:
        raise IdentityExperimentError("delegation audiences are required")
    normalized = sorted({_identifier(item, "audience", 256) for item in audiences})
    if expires_at <= issued_at:
        raise IdentityExperimentError("delegation expiry must be after issuance")
    payload = {
        "version": DELEGATION_VERSION,
        "root_id": root.root_id,
        "root_public_key": root.public_key,
        "delegation_id": delegation_id,
        "delegate_public_key": delegate.public_key,
        "audiences": normalized,
        "capabilities": [AUTH_CAPABILITY],
        "issued_at": float(issued_at),
        "expires_at": float(expires_at),
    }
    return {"payload": payload, "signature": root.sign(payload)}


def create_delegation_revocation(
    root: Ed25519Identity,
    delegation_id: str,
    *,
    revoked_at: float,
) -> dict[str, Any]:
    payload = {
        "version": REVOCATION_VERSION,
        "root_id": root.root_id,
        "root_public_key": root.public_key,
        "delegation_id": _identifier(delegation_id, "delegation_id"),
        "revoked_at": float(revoked_at),
    }
    return {"payload": payload, "signature": root.sign(payload)}


def create_root_rotation(
    old_root: Ed25519Identity,
    new_root: Ed25519Identity,
    *,
    issued_at: float,
    invalidate_prior_delegations: bool = True,
) -> dict[str, Any]:
    payload = {
        "version": ROTATION_VERSION,
        "old_root_id": old_root.root_id,
        "old_public_key": old_root.public_key,
        "new_root_id": new_root.root_id,
        "new_public_key": new_root.public_key,
        "issued_at": float(issued_at),
        "invalidate_prior_delegations": bool(invalidate_prior_delegations),
    }
    return {
        "payload": payload,
        "old_signature": old_root.sign(payload),
        "new_signature": new_root.sign(payload),
    }


def sign_challenge(
    delegate: Ed25519Identity,
    delegation: dict[str, Any],
    challenge: dict[str, Any],
    *,
    issued_at: float,
    audience_override: str | None = None,
) -> dict[str, Any]:
    delegation_payload = delegation["payload"]
    payload = {
        "version": PROOF_VERSION,
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "audience": audience_override or challenge["audience"],
        "root_id": delegation_payload["root_id"],
        "delegation_id": delegation_payload["delegation_id"],
        "delegate_public_key": delegation_payload["delegate_public_key"],
        "issued_at": float(issued_at),
    }
    return {
        "payload": payload,
        "signature": delegate.sign(payload),
        "delegation": delegation,
    }


class IdentityProofDeployment:
    """One independent world's verifier + its own local Runtime credential issuer."""

    def __init__(
        self,
        *,
        deployment_id: str,
        universe: str,
        runtime_db: str | Path,
        identity_db: str | Path,
        clock: Callable[[], float] = time.time,
    ):
        self.deployment_id = _identifier(deployment_id, "deployment_id", 128)
        self.universe = _identifier(universe, "universe", 128)
        self.audience = f"agent-world:{self.deployment_id}:{self.universe}"
        self.runtime_db = Path(runtime_db)
        self.identity_db = Path(identity_db)
        self.runtime_db.parent.mkdir(parents=True, exist_ok=True)
        self.identity_db.parent.mkdir(parents=True, exist_ok=True)
        self.runtime = WorldRuntime(self.runtime_db)
        self._clock = clock
        self._lock = threading.RLock()
        self._init_store()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.identity_db, timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _init_store(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_identities(
                  identity_id TEXT PRIMARY KEY,
                  role_id TEXT NOT NULL UNIQUE,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS root_keys(
                  root_id TEXT PRIMARY KEY,
                  identity_id TEXT NOT NULL,
                  public_key TEXT NOT NULL,
                  status TEXT NOT NULL,
                  added_at REAL NOT NULL,
                  superseded_at REAL,
                  superseded_by TEXT,
                  FOREIGN KEY(identity_id) REFERENCES local_identities(identity_id)
                );
                CREATE TABLE IF NOT EXISTS challenges(
                  challenge_id TEXT PRIMARY KEY,
                  nonce TEXT NOT NULL,
                  audience TEXT NOT NULL,
                  issued_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  used_at REAL
                );
                CREATE TABLE IF NOT EXISTS delegation_revocations(
                  root_id TEXT NOT NULL,
                  delegation_id TEXT NOT NULL,
                  revoked_at REAL NOT NULL,
                  PRIMARY KEY(root_id,delegation_id)
                );
                CREATE TABLE IF NOT EXISTS issued_credentials(
                  token_id TEXT PRIMARY KEY,
                  root_id TEXT NOT NULL,
                  delegation_id TEXT NOT NULL,
                  issued_at REAL NOT NULL,
                  expires_at REAL
                );
                """
            )

    def issue_challenge(self, *, ttl_seconds: float = 60) -> dict[str, Any]:
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ChallengeRejected("challenge ttl must be within 0..300 seconds")
        now = float(self._clock())
        challenge = {
            "version": CHALLENGE_VERSION,
            "challenge_id": "awch_" + secrets.token_hex(16),
            "nonce": _b64e(secrets.token_bytes(32)),
            "audience": self.audience,
            "issued_at": now,
            "expires_at": now + float(ttl_seconds),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO challenges(
                     challenge_id,nonce,audience,issued_at,expires_at,used_at
                   ) VALUES(?,?,?,?,?,NULL)""",
                (
                    challenge["challenge_id"],
                    challenge["nonce"],
                    challenge["audience"],
                    challenge["issued_at"],
                    challenge["expires_at"],
                ),
            )
        return challenge

    def _verify_delegation(
        self,
        delegation: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        try:
            payload = dict(delegation["payload"])
            signature = delegation["signature"]
        except (KeyError, TypeError, ValueError) as exc:
            raise DelegationRejected("malformed delegation") from exc
        if payload.get("version") != DELEGATION_VERSION:
            raise DelegationRejected("unsupported delegation version")
        root_public_key = payload.get("root_public_key")
        try:
            derived_root_id = root_id_from_public_key(root_public_key)
        except IdentityExperimentError as exc:
            raise DelegationRejected(str(exc)) from exc
        if payload.get("root_id") != derived_root_id:
            raise DelegationRejected("delegation root id does not match public key")
        _identifier(str(payload.get("delegation_id", "")), "delegation_id")
        delegate_public_key = payload.get("delegate_public_key")
        _load_public_key(delegate_public_key, "delegate public key")
        audiences = payload.get("audiences")
        if not isinstance(audiences, list) or self.audience not in audiences:
            raise DelegationRejected("delegation is not valid for this audience")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or AUTH_CAPABILITY not in capabilities:
            raise DelegationRejected("delegation cannot authenticate")
        try:
            issued_at = float(payload["issued_at"])
            expires_at = float(payload["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DelegationRejected("invalid delegation time bounds") from exc
        if issued_at > now + 5:
            raise DelegationRejected("delegation was issued in the future")
        if expires_at <= now:
            raise DelegationRejected("delegation has expired")
        if expires_at <= issued_at:
            raise DelegationRejected("invalid delegation lifetime")
        try:
            _verify(root_public_key, signature, payload, "root delegation")
        except IdentityExperimentError as exc:
            raise DelegationRejected(str(exc)) from exc
        return payload

    def _verify_proof(
        self,
        proof: dict[str, Any],
        challenge: sqlite3.Row,
        *,
        now: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            payload = dict(proof["payload"])
            signature = proof["signature"]
            delegation = proof["delegation"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProofRejected("malformed identity proof") from exc
        if payload.get("version") != PROOF_VERSION:
            raise ProofRejected("unsupported identity proof version")
        if challenge["used_at"] is not None:
            raise ChallengeRejected("challenge was already used")
        if float(challenge["expires_at"]) <= now:
            raise ChallengeRejected("challenge has expired")
        expected = {
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "audience": challenge["audience"],
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ChallengeRejected(f"proof {key} does not match issued challenge")
        try:
            proof_issued_at = float(payload["issued_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProofRejected("invalid proof issued_at") from exc
        if proof_issued_at < float(challenge["issued_at"]) - 5 or proof_issued_at > now + 5:
            raise ProofRejected("proof issued_at is outside challenge window")

        delegation_payload = self._verify_delegation(delegation, now=now)
        for key in ("root_id", "delegation_id", "delegate_public_key"):
            if payload.get(key) != delegation_payload.get(key):
                raise ProofRejected(f"proof {key} does not match delegation")
        try:
            _verify(
                delegation_payload["delegate_public_key"],
                signature,
                payload,
                "delegate proof",
            )
        except IdentityExperimentError as exc:
            raise ProofRejected(str(exc)) from exc
        return payload, delegation_payload

    def _known_root(self, connection: sqlite3.Connection, root_id: str):
        return connection.execute(
            """SELECT r.root_id,r.identity_id,r.public_key,r.status,
                      i.role_id
               FROM root_keys r
               JOIN local_identities i ON i.identity_id=r.identity_id
               WHERE r.root_id=?""",
            (root_id,),
        ).fetchone()

    def _ensure_local_identity(
        self,
        root_id: str,
        root_public_key: str,
        display_name: str,
    ) -> str:
        with self._connect() as connection:
            existing = self._known_root(connection, root_id)
        if existing is not None:
            if existing["status"] != "active":
                raise DelegationRejected("root identity has been superseded")
            if existing["public_key"] != root_public_key:
                raise DelegationRejected("stored root public key mismatch")
            return str(existing["role_id"])

        role = self.runtime.create_role(display_name)
        identity_id = "awli_" + secrets.token_hex(16)
        now = float(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._known_root(connection, root_id)
                if existing is not None:
                    connection.execute("COMMIT")
                    self.runtime.set_role_status(role["role_id"], "disabled")
                    if existing["status"] != "active":
                        raise DelegationRejected("root identity has been superseded")
                    return str(existing["role_id"])
                connection.execute(
                    "INSERT INTO local_identities(identity_id,role_id,created_at) VALUES(?,?,?)",
                    (identity_id, role["role_id"], now),
                )
                connection.execute(
                    """INSERT INTO root_keys(
                         root_id,identity_id,public_key,status,added_at,superseded_at,superseded_by
                       ) VALUES(?,?,?,'active',?,NULL,NULL)""",
                    (root_id, identity_id, root_public_key, now),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                try:
                    self.runtime.set_role_status(role["role_id"], "disabled")
                except Exception:
                    pass
                raise
        return str(role["role_id"])

    def authenticate(
        self,
        proof: dict[str, Any],
        *,
        display_name: str,
        token_ttl_seconds: float = 3600,
    ) -> dict[str, Any]:
        _identifier(display_name.strip(), "display_name", 80)
        now = float(self._clock())
        with self._lock:
            try:
                challenge_id = proof["payload"]["challenge_id"]
            except (KeyError, TypeError) as exc:
                raise ProofRejected("proof is missing challenge_id") from exc
            with self._connect() as connection:
                challenge = connection.execute(
                    "SELECT * FROM challenges WHERE challenge_id=?",
                    (challenge_id,),
                ).fetchone()
            if challenge is None:
                raise ChallengeRejected("unknown challenge")

            proof_payload, delegation_payload = self._verify_proof(
                proof,
                challenge,
                now=now,
            )
            root_id = delegation_payload["root_id"]
            root_public_key = delegation_payload["root_public_key"]
            delegation_id = delegation_payload["delegation_id"]

            with self._connect() as connection:
                known = self._known_root(connection, root_id)
                if known is not None:
                    if known["status"] != "active":
                        raise DelegationRejected("root identity has been superseded")
                    if known["public_key"] != root_public_key:
                        raise DelegationRejected("stored root public key mismatch")
                revoked = connection.execute(
                    """SELECT 1 FROM delegation_revocations
                       WHERE root_id=? AND delegation_id=?""",
                    (root_id, delegation_id),
                ).fetchone()
                if revoked is not None:
                    raise DelegationRejected("delegation has been revoked")

            role_id = self._ensure_local_identity(
                root_id,
                root_public_key,
                display_name.strip(),
            )

            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute(
                        "SELECT used_at,expires_at FROM challenges WHERE challenge_id=?",
                        (challenge_id,),
                    ).fetchone()
                    if current is None:
                        raise ChallengeRejected("unknown challenge")
                    if current["used_at"] is not None:
                        raise ChallengeRejected("challenge was already used")
                    if float(current["expires_at"]) <= now:
                        raise ChallengeRejected("challenge has expired")
                    connection.execute(
                        "UPDATE challenges SET used_at=? WHERE challenge_id=?",
                        (now, challenge_id),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise

            credential = self.runtime.issue_identity_token(
                self.universe,
                role_id,
                ttl_seconds=token_ttl_seconds,
            )
            try:
                with self._connect() as connection:
                    connection.execute(
                        """INSERT INTO issued_credentials(
                             token_id,root_id,delegation_id,issued_at,expires_at
                           ) VALUES(?,?,?,?,?)""",
                        (
                            credential["token_id"],
                            root_id,
                            delegation_id,
                            credential["created_at"],
                            credential["expires_at"],
                        ),
                    )
            except Exception:
                self.runtime.revoke_identity_token(
                    credential["token_id"],
                    expected_universe=self.universe,
                )
                raise
            return {
                "root_id": root_id,
                "delegation_id": delegation_id,
                "audience": proof_payload["audience"],
                "role": self.runtime.get_role(role_id),
                "credential": credential,
            }

    def accept_delegation_revocation(self, revocation: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = dict(revocation["payload"])
            signature = revocation["signature"]
        except (KeyError, TypeError, ValueError) as exc:
            raise DelegationRejected("malformed delegation revocation") from exc
        if payload.get("version") != REVOCATION_VERSION:
            raise DelegationRejected("unsupported delegation revocation version")
        root_public_key = payload.get("root_public_key")
        root_id = payload.get("root_id")
        if root_id != root_id_from_public_key(root_public_key):
            raise DelegationRejected("revocation root id does not match public key")
        delegation_id = _identifier(
            str(payload.get("delegation_id", "")),
            "delegation_id",
        )
        try:
            _verify(root_public_key, signature, payload, "delegation revocation")
        except IdentityExperimentError as exc:
            raise DelegationRejected(str(exc)) from exc
        with self._lock, self._connect() as connection:
            known = self._known_root(connection, root_id)
            if known is None or known["public_key"] != root_public_key:
                raise DelegationRejected("revocation root is not known to this deployment")
            connection.execute(
                """INSERT INTO delegation_revocations(root_id,delegation_id,revoked_at)
                   VALUES(?,?,?)
                   ON CONFLICT(root_id,delegation_id)
                   DO UPDATE SET revoked_at=excluded.revoked_at""",
                (root_id, delegation_id, float(payload["revoked_at"])),
            )
            token_ids = [
                str(row["token_id"])
                for row in connection.execute(
                    """SELECT token_id FROM issued_credentials
                       WHERE root_id=? AND delegation_id=?""",
                    (root_id, delegation_id),
                )
            ]
        revoked_tokens = 0
        for token_id in token_ids:
            try:
                if self.runtime.revoke_identity_token(
                    token_id,
                    expected_universe=self.universe,
                ):
                    revoked_tokens += 1
            except InvalidIdentityToken:
                pass
        return {
            "root_id": root_id,
            "delegation_id": delegation_id,
            "revoked_tokens": revoked_tokens,
        }

    def accept_root_rotation(self, rotation: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = dict(rotation["payload"])
            old_signature = rotation["old_signature"]
            new_signature = rotation["new_signature"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RootRotationRejected("malformed root rotation") from exc
        if payload.get("version") != ROTATION_VERSION:
            raise RootRotationRejected("unsupported root rotation version")
        old_public_key = payload.get("old_public_key")
        new_public_key = payload.get("new_public_key")
        old_root_id = payload.get("old_root_id")
        new_root_id = payload.get("new_root_id")
        if old_root_id != root_id_from_public_key(old_public_key):
            raise RootRotationRejected("old root id does not match public key")
        if new_root_id != root_id_from_public_key(new_public_key):
            raise RootRotationRejected("new root id does not match public key")
        if old_root_id == new_root_id:
            raise RootRotationRejected("root rotation must change the key")
        try:
            _verify(old_public_key, old_signature, payload, "old root rotation")
            _verify(new_public_key, new_signature, payload, "new root rotation")
        except IdentityExperimentError as exc:
            raise RootRotationRejected(str(exc)) from exc

        now = float(self._clock())
        if float(payload["issued_at"]) > now + 5:
            raise RootRotationRejected("root rotation was issued in the future")

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = self._known_root(connection, old_root_id)
                if old is None:
                    raise RootRotationRejected("old root is not known to this deployment")
                existing_new = self._known_root(connection, new_root_id)
                if old["status"] == "superseded":
                    if (
                        old["identity_id"] == (
                            existing_new["identity_id"] if existing_new is not None else None
                        )
                        and old["public_key"] == old_public_key
                        and existing_new["public_key"] == new_public_key
                    ):
                        connection.execute("COMMIT")
                        return {
                            "old_root_id": old_root_id,
                            "new_root_id": new_root_id,
                            "role_id": old["role_id"],
                            "replayed": True,
                        }
                    raise RootRotationRejected("old root was already superseded differently")
                if old["public_key"] != old_public_key:
                    raise RootRotationRejected("stored old root public key mismatch")
                if existing_new is not None:
                    raise RootRotationRejected("new root is already bound")

                connection.execute(
                    """INSERT INTO root_keys(
                         root_id,identity_id,public_key,status,added_at,superseded_at,superseded_by
                       ) VALUES(?,?,?,'active',?,NULL,NULL)""",
                    (new_root_id, old["identity_id"], new_public_key, now),
                )
                connection.execute(
                    """UPDATE root_keys
                       SET status='superseded',superseded_at=?,superseded_by=?
                       WHERE root_id=?""",
                    (now, new_root_id, old_root_id),
                )
                token_ids = []
                if payload.get("invalidate_prior_delegations") is True:
                    token_ids = [
                        str(row["token_id"])
                        for row in connection.execute(
                            "SELECT token_id FROM issued_credentials WHERE root_id=?",
                            (old_root_id,),
                        )
                    ]
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        revoked_tokens = 0
        for token_id in token_ids:
            try:
                if self.runtime.revoke_identity_token(
                    token_id,
                    expected_universe=self.universe,
                ):
                    revoked_tokens += 1
            except InvalidIdentityToken:
                pass
        return {
            "old_root_id": old_root_id,
            "new_root_id": new_root_id,
            "role_id": str(old["role_id"]),
            "replayed": False,
            "revoked_tokens": revoked_tokens,
        }

    def lookup_role(self, root_id: str) -> str | None:
        with self._connect() as connection:
            row = self._known_root(connection, root_id)
        return None if row is None else str(row["role_id"])

    def root_status(self, root_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM root_keys WHERE root_id=?",
                (root_id,),
            ).fetchone()
        return None if row is None else str(row["status"])
