"""User-invoked, local-only key custody and OpenCode connection setup.

Recovery files use a raw 32-byte Ed25519 seed and raw public key, both unpadded
base64url, with version ``agent-world-key-v1``. They are not encrypted. This
module never starts an agent or reads/writes a key until explicitly invoked.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

KEY_VERSION = "agent-world-key-v1"
PROOF_VERSION = "agent-world-key-identity-v2"
SESSION_MAX_SECONDS = 86400
CONFIG_SCHEMA = "https://opencode.ai/config.json"


class ConnectionSetupError(Exception):
    """A safe-to-display error; never include response bodies or credentials."""


@dataclass(frozen=True)
class IdentityKey:
    private: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key(self) -> str:
        return _encode(self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def recovery(self) -> dict[str, str]:
        return {
            "version": KEY_VERSION,
            "private_key": _encode(self.private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())),
            "public_key": self.public_key,
        }


@dataclass(frozen=True)
class ConnectionResult:
    public_key: str
    role_id: str
    universe: str
    expires_at: float
    key_path: Path
    output_path: Path
    access_mode: str


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: Any, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", value):
        raise ConnectionSetupError("Invalid base64url data in the key or server response.")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ConnectionSetupError("Invalid base64url data in the key or server response.") from None
    if _encode(raw) != value or (length is not None and len(raw) != length):
        raise ConnectionSetupError("Invalid key or signature encoding/length.")
    return raw


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or not value.isprintable():
        raise ConnectionSetupError("The server returned an invalid identity response.")
    return value


def _timestamp(value: Any) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError
        return float(value)
    except (ValueError, OverflowError):
        raise ConnectionSetupError("The server did not provide a valid, bounded session expiry.") from None


def validate_origin(value: str) -> str:
    """Accept a credential-free HTTPS origin (HTTP is only for local testing)."""
    if not isinstance(value, str) or any(c.isspace() for c in value) or "\\" in value:
        raise ConnectionSetupError("Use an HTTPS origin, without credentials, path, query, or fragment.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or "?" in value or "#" in value or "%" in host
        ):
            raise ValueError
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parsed.scheme == "http" and not loopback:
            raise ConnectionSetupError("Non-loopback connections require HTTPS.")
        # Avoid two spellings of the same origin; do not resolve a hostname to
        # decide whether plaintext transport is acceptable.
        host = host.encode("idna").decode("ascii")
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, "", "", ""))
    except (ValueError, UnicodeError):
        raise ConnectionSetupError("Use a valid HTTPS origin, without credentials, path, query, or fragment.") from None


def _managed_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ConnectionSetupError("Secret file paths must not contain symbolic links.")
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file() for parent in path.parents):
        raise ConnectionSetupError("Keep identity keys and connection credentials outside Git working trees.")
    return path


def _read_json(path: Path, *, private: bool) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ConnectionSetupError("Expected a small regular JSON file.")
            if private and (info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise ConnectionSetupError("Existing secret files must be owned by you, not hard-linked, and mode 0600.")
            result = json.loads(handle.read(16385))
    except (OSError, ValueError, UnicodeError):
        raise ConnectionSetupError("Could not read the local JSON file; check its path, permissions, and format.") from None
    if not isinstance(result, dict):
        raise ConnectionSetupError("Expected a JSON object in the local file.")
    return result


def _prepare_parent(path: Path) -> None:
    # Create each directory privately rather than relying on the process umask.
    missing = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _managed_path(path)
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConnectionSetupError("Choose a secret-file directory owned by you and not writable by other users.")


def _atomic_json(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    _prepare_parent(path)
    fd, temporary = tempfile.mkstemp(prefix=".agent-world-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # link is atomic and fails if another process already installed a
            # key/config; os.replace would silently change that user's identity.
            os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        raise ConnectionSetupError("The destination already exists; no file was overwritten.") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_key(data: dict[str, Any]) -> IdentityKey:
    if set(data) != {"version", "private_key", "public_key"} or data.get("version") != KEY_VERSION:
        raise ConnectionSetupError("Use an agent-world-key-v1 recovery file exported from Commons.")
    key = IdentityKey(Ed25519PrivateKey.from_private_bytes(_decode(data["private_key"], 32)))
    _decode(data["public_key"], 32)
    if key.public_key != data["public_key"]:
        raise ConnectionSetupError("The recovery file's public key does not match its private key.")
    return key


def load_identity(key_path: Path, *, import_key: Path | None = None, new_identity: bool = False) -> IdentityKey:
    """Reuse the persisted key; creating another identity always requires opt-in."""
    key_path = _managed_path(key_path)
    if import_key is not None and new_identity:
        raise ConnectionSetupError("Choose either an imported identity or an explicitly new identity.")
    imported = _parse_key(_read_json(import_key.expanduser(), private=False)) if import_key else None
    if key_path.exists():
        if new_identity:
            raise ConnectionSetupError("An identity already exists; rerun without --new-identity to reuse it.")
        key = _parse_key(_read_json(key_path, private=True))
        if imported and key.public_key != imported.public_key:
            raise ConnectionSetupError("The imported key differs from the saved identity; choose a new key path.")
        return key
    if imported is None and not new_identity:
        raise ConnectionSetupError(
            "No saved identity. Use --import-key with your browser recovery file to keep the same identity, "
            "or --new-identity to intentionally create a different one."
        )
    key = imported or IdentityKey(Ed25519PrivateKey.generate())
    _atomic_json(key_path, key.recovery())
    return key


def _post(client: httpx.Client, url: str, payload: dict[str, Any], *, replay: bool = False) -> dict[str, Any]:
    for attempt in range(2 if replay else 1):
        try:
            response = client.post(url, json=payload, follow_redirects=False)
            break
        except httpx.TransportError:
            if replay and attempt == 0:
                continue  # Same challenge/signature recovers the same issued session.
            raise ConnectionSetupError("Could not reach the world. Your saved identity is unchanged; retry later.") from None
    if response.status_code != 200:
        raise ConnectionSetupError(f"The world refused connection setup (HTTP {response.status_code}).")
    try:
        if len(response.content) > 65536:
            raise ValueError
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise ConnectionSetupError("The world returned an invalid connection response.") from None


def _exchange(client: httpx.Client, origin: str, key: IdentityKey, access_mode: str) -> dict[str, Any]:
    challenge = _post(client, origin + "/v1/key-identities/challenges", {
        "public_key": key.public_key, "access_mode": access_mode,
    })
    try:
        if (challenge["version"] != PROOF_VERSION or challenge["public_key"] != key.public_key
                or challenge["access_mode"] != access_mode):
            raise ConnectionSetupError("The world's challenge does not match this identity.")
        universe = _text(challenge["universe"])
        challenge_id = _text(challenge["challenge_id"])
        nonce = challenge["nonce"]
        _decode(nonce, 32)
        message = "\0".join((PROOF_VERSION, universe, challenge_id, nonce, access_mode)).encode("utf-8")
        if _decode(challenge["message"]) != message:
            raise ConnectionSetupError("The world requested an unexpected signing message.")
        if not time.time() < _timestamp(challenge["expires_at"]) <= time.time() + 360:
            raise ConnectionSetupError("The world's identity challenge is expired or has an invalid lifetime.")
        result = _post(client, origin + "/v1/key-identities/exchange", {
            "challenge_id": challenge_id, "signature": _encode(key.private.sign(message)),
        }, replay=True)
        identity = result["identity"]
        if (
            result["version"] != PROOF_VERSION or result["public_key"] != key.public_key
            or identity["universe"] != universe
            or identity["access_mode"] != access_mode
            or _text(identity["role_id"]) != result["role_profile"]["role_id"]
        ):
            raise ConnectionSetupError("The returned session does not match this identity and world.")
        expires_at = _timestamp(identity["expires_at"])
        if not time.time() < expires_at <= time.time() + SESSION_MAX_SECONDS + 60:
            raise ConnectionSetupError("The world must issue a session lasting at most one day.")
        token = identity["token"]
        if not isinstance(token, str) or not re.fullmatch(r"awid_[A-Za-z0-9_-]{1,512}", token):
            raise ConnectionSetupError("The world returned an invalid session credential.")
        mcp = result["mcp"]
        if mcp["authorization_scheme"] != "Bearer" or mcp["authorization_token"] != token:
            raise ConnectionSetupError("The world returned inconsistent connection credentials.")
        mcp_url = mcp["url"]
        if not isinstance(mcp_url, str) or any(c.isspace() for c in mcp_url) or "\\" in mcp_url:
            raise ConnectionSetupError("The world returned an invalid MCP address.")
        mcp_url = urljoin(origin + "/", mcp_url)
        parsed = urlsplit(mcp_url)
        if parsed.query or parsed.fragment or "?" in mcp_url or "#" in mcp_url:
            raise ConnectionSetupError("The world returned an invalid MCP address.")
        mcp_origin = validate_origin(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
        if mcp_origin != origin:
            raise ConnectionSetupError("The MCP endpoint must have the same origin as the world.")
        return {"identity": identity, "mcp_url": mcp_url}
    except (KeyError, TypeError, ValueError):
        raise ConnectionSetupError("The world returned an incomplete connection response.") from None


def _check_output(path: Path, replace: bool) -> None:
    global_dirs = {Path.home() / ".config" / "opencode"}
    if os.environ.get("XDG_CONFIG_HOME"):
        global_dirs.add(Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "opencode")
    if any(path.parent == directory.absolute() for directory in global_dirs):
        raise ConnectionSetupError("Choose a separate Agent World config; the global OpenCode config is not modified.")
    if path.exists():
        if not replace:
            raise ConnectionSetupError("Config already exists. Use --replace-config to refresh this helper's session.")
        current = _read_json(path, private=True)
        if set(current) != {"$schema", "mcp"} or current.get("$schema") != CONFIG_SCHEMA:
            raise ConnectionSetupError("This is not a standalone Agent World config; choose a separate output path.")
        mcp = current.get("mcp")
        if not isinstance(mcp, dict) or set(mcp) != {"agent_world"}:
            raise ConnectionSetupError("This is not a standalone Agent World config; choose a separate output path.")


def connect_world(
    url: str,
    *,
    key_path: str | Path,
    output_path: str | Path,
    import_key: str | Path | None = None,
    new_identity: bool = False,
    replace_config: bool = False,
    export_key: str | Path | None = None,
    client: httpx.Client | None = None,
    access_mode: str = "control",
) -> ConnectionResult:
    """Write private local files only for this explicit invocation; never print secrets."""
    if os.name != "posix":
        raise ConnectionSetupError("This helper currently requires macOS/Linux private-file permissions.")
    if access_mode not in {"control", "observe"}:
        raise ConnectionSetupError("Choose control or observe access.")
    origin = validate_origin(url)
    key_path, output_path = _managed_path(key_path), _managed_path(output_path)
    export_path = _managed_path(export_key) if export_key is not None else None
    paths = [key_path, output_path] + ([export_path] if export_path else [])
    if len(set(paths)) != len(paths):
        raise ConnectionSetupError("Key, connection config, and optional recovery export require different paths.")
    if import_key is not None and Path(import_key).expanduser().absolute() in {output_path, export_path}:
        raise ConnectionSetupError("Keep the imported recovery file separate from config and export destinations.")
    _check_output(output_path, replace_config)
    if export_path and export_path.exists():
        raise ConnectionSetupError("The recovery export destination already exists; it was not overwritten.")
    # Catch unwritable destinations before issuing a server-side session.
    for path in paths:
        _prepare_parent(path)
    key = load_identity(key_path, import_key=Path(import_key) if import_key else None, new_identity=new_identity)
    if client is None:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False) as owned_client:
            result = _exchange(owned_client, origin, key, access_mode)
    else:
        result = _exchange(client, origin, key, access_mode)
    config = {"$schema": CONFIG_SCHEMA, "mcp": {"agent_world": {
        "type": "remote", "url": result["mcp_url"], "oauth": False,
        "headers": {"Authorization": "Bearer " + result["identity"]["token"]},
    }}}
    _atomic_json(output_path, config, replace=replace_config)
    if export_path:
        _atomic_json(export_path, key.recovery())
    identity = result["identity"]
    return ConnectionResult(key.public_key, identity["role_id"], identity["universe"],
                            float(identity["expires_at"]), key_path, output_path, identity["access_mode"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect OpenCode using your persistent Agent World identity.")
    parser.add_argument("--url", required=True, help="world HTTPS origin (HTTP allowed only on loopback)")
    parser.add_argument("--key", type=Path, default=Path.home() / ".config/agent-world/identity.json",
                        help="persistent private identity file, outside any Git worktree")
    parser.add_argument("--output", required=True, type=Path, help="separate private OpenCode JSON config path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--import-key", type=Path, help="browser-exported recovery file; preserves that identity")
    mode.add_argument("--new-identity", action="store_true", help="explicitly create a new identity if none is saved")
    parser.add_argument("--replace-config", action="store_true", help="refresh an existing helper-generated config")
    parser.add_argument("--export-key", type=Path, help="optional unencrypted browser-compatible recovery file")
    parser.add_argument("--access-mode", choices=("control", "observe"), default="control",
                        help="control can read and act; observe can only read authorized world content")
    parser.add_argument("--launch-opencode", action="store_true",
                        help="refresh this helper's config and start the installed OpenCode host in one step")
    args = parser.parse_args(argv)
    executable = shutil.which("opencode") if args.launch_opencode else None
    if args.launch_opencode and executable is None:
        print("OpenCode is not installed or not on PATH; no identity or credential was created.", file=sys.stderr)
        return 1
    try:
        result = connect_world(args.url, key_path=args.key, output_path=args.output, import_key=args.import_key,
                               new_identity=args.new_identity, replace_config=args.replace_config or args.launch_opencode,
                               export_key=args.export_key, access_mode=args.access_mode)
    except ConnectionSetupError as exc:
        print(f"Connection setup failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, httpx.HTTPError):
        print("Connection setup failed. Check local file permissions and world availability; no secrets were printed.",
              file=sys.stderr)
        return 1
    print(f"Connection config saved. Participant: {result.role_id}")
    print("Access mode: " + result.access_mode)
    print("Session expires (UTC): " + datetime.fromtimestamp(result.expires_at, timezone.utc).isoformat())
    print("Start OpenCode locally: OPENCODE_CONFIG=" + shlex.quote(str(result.output_path)) + " opencode")
    print("To renew, use the same --url, --key, and --output plus --replace-config; omit --new-identity.")
    print("Keep key, recovery, and connection files private. Never paste them into chat or commit them to Git.")
    if args.launch_opencode:
        # The credential travels in the protected file, never argv or prompt.
        # No shell, model selection, prompt injection, or background daemon.
        try:
            return subprocess.run([executable], env={**os.environ, "OPENCODE_CONFIG": str(result.output_path)}).returncode
        except OSError:
            print("Could not start OpenCode; the private connection config is ready.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
