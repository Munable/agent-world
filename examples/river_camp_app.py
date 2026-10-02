"""Loopback-only reference consumer. World rules live in river_camp, not the Runtime."""
from __future__ import annotations

import argparse
import base64
from contextlib import asynccontextmanager, closing
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
import stat

from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agent_world import WorldRuntime
from agent_world.application import create_application
from agent_world.connect import load_identity
from agent_world.world_sdk import install_world


def _storage_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Use real storage paths, not symbolic links, for local world data.")
    return path


def _check_private_directory(path: Path) -> None:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Use a directory owned by you with permissions 0700; existing permissions are never changed.")


def _existing_camp(db_path: Path, universe: str) -> Path:
    """Reject aliases/unsafe files and wrong instances before the default installer runs."""
    db_path = _storage_path(db_path)
    if not db_path.exists():
        raise ValueError("Database is missing. Use --init-local to explicitly provision this local reference world.")
    _check_private_directory(db_path.parent)
    info = db_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ValueError("Reuse needs a regular database owned by you, without hard links, with permissions 0600.")
    # Runtime construction installs its storage schema. A rejected CLI reuse
    # must leave the existing file untouched, so this local preflight is read-only.
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT manifest_json FROM world_definitions WHERE universe=?", (universe,)).fetchone()
        manifest = json.loads(row[0]) if row is not None else None
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise ValueError("The existing file is not a readable Agent World database.") from exc
    if not isinstance(manifest, dict) or manifest.get("world_id") != "river-camp-example":
        raise ValueError("The requested universe is not an existing River Camp instance; check --db and --universe.")
    return db_path


def initialize_local(db_path: Path, private_dir: Path, universe: str) -> dict:
    """Explicit trusted local provisioning, never an HTTP or Agent action."""
    previous_umask = os.umask(0o077)
    try:
        return _provision_local(db_path, private_dir, universe)
    finally:
        os.umask(previous_umask)


def _provision_local(db_path: Path, private_dir: Path, universe: str) -> dict:
    from .river_camp import make_world

    db_path = _storage_path(db_path)
    private_dir = _storage_path(private_dir)
    if db_path.exists():
        raise ValueError("Initialization needs a new database; existing worlds are never reset.")
    if any((parent / ".git").exists() for parent in (private_dir, *private_dir.parents)):
        raise ValueError("Keep the private identity directory outside Git working trees.")
    if db_path.parent.exists():
        _check_private_directory(db_path.parent)
    if private_dir.exists():
        _check_private_directory(private_dir)
        if any(private_dir.iterdir()):
            raise ValueError("Initialization needs an empty private directory; existing files are never overwritten.")
    metadata = private_dir / "setup.json"
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_private_directory(db_path.parent)
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_private_directory(private_dir)
    runtime = WorldRuntime(db_path)
    participants = {}
    for label, display in (
        ("Founder", "Local world steward"), ("GM", "Camp referee"),
        ("A", "Safety explorer"), ("B", "Material keeper"), ("C", "Route explorer"),
    ):
        folder = private_dir / label
        folder.mkdir(mode=0o700)
        key_path = folder / "identity.json"
        key = load_identity(key_path, new_identity=True)
        challenge = runtime.issue_identity_key_challenge(universe, key.public_key)
        message = base64.urlsafe_b64decode(challenge["message"] + "=" * (-len(challenge["message"]) % 4))
        signature = base64.urlsafe_b64encode(key.private.sign(message)).rstrip(b"=").decode("ascii")
        joined = runtime.exchange_identity_key_challenge(
            challenge["challenge_id"], signature, expected_universe=universe,
        )
        role_id = joined["role_profile"]["role_id"]
        runtime.update_role_profile(role_id, display_name=display)
        runtime.revoke_identity_token(joined["identity"]["token_id"])
        participants[label] = {"role_id": role_id, "key_path": str(key_path)}
    install_world(runtime, universe, make_world(
        founder_role_id=participants["Founder"]["role_id"],
        judge_role_ids=(participants["GM"]["role_id"],),
    ))
    result = {"database": str(db_path), "universe": universe, "participants": participants}
    with metadata.open("x", encoding="utf-8") as out:
        os.chmod(metadata, 0o600)
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    os.chmod(db_path, 0o600)
    return result


def create_camp_application(db_path: Path, *, universe="river-camp", port=8770):
    inner = create_application(
        db_path, world_profile="examples.river_camp:WORLD", universe=universe,
        public_base_url=f"http://127.0.0.1:{port}",
    )
    web = files("examples").joinpath("river_camp_web")

    async def home(request):
        return FileResponse(web.joinpath("index.html"), headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    @asynccontextmanager
    async def lifespan(app):
        async with inner.router.lifespan_context(inner):
            yield

    return Starlette(routes=[
        Route("/", home), Mount("/camp-static", StaticFiles(directory=str(web))),
        Mount("/", inner),
    ], lifespan=lifespan)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Run the isolated River Camp reference world.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--universe", default="river-camp")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--init-local", action="store_true", help="Provision synthetic local identities in a new world.")
    parser.add_argument("--private-dir", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.init_local:
        if args.private_dir is None:
            parser.error("--init-local requires --private-dir outside the repository")
        initialize_local(args.db, args.private_dir, args.universe)
        print("Local identities provisioned; private recovery files are in the requested private directory.")
    else:
        try:
            args.db = _existing_camp(args.db, args.universe)
        except ValueError as exc:
            parser.error(str(exc))
    import uvicorn

    uvicorn.run(create_camp_application(args.db, universe=args.universe, port=args.port),
                host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
