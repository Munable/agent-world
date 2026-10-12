"""Explicitly provision and serve an isolated, loopback-only D&D example."""
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

WORLD_ID = "old-lighthouse-dnd"
UNIVERSE = "old-lighthouse"
PROFILE = "examples.dnd_world:WORLD"


def _path(value: Path) -> Path:
    value = value.expanduser().absolute()
    if any(p.is_symlink() for p in (value, *value.parents)):
        raise ValueError("Use real paths without symbolic links for local world data.")
    return value


def _private_directory(value: Path) -> None:
    info = value.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("World data directories must be owned by you and have permissions 0700.")


def existing_world(db: Path, universe: str) -> Path:
    db = _path(db)
    if not db.exists():
        raise ValueError("World database is missing; provision a new local world with --init-local.")
    _private_directory(db.parent)
    info = db.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ValueError("Use a regular, privately owned database with permissions 0600 and no hard links.")
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT manifest_json FROM world_definitions WHERE universe=?", (universe,)).fetchone()
        manifest = json.loads(row[0]) if row else {}
    except (ValueError, sqlite3.DatabaseError) as exc:
        raise ValueError("The file is not a readable Agent World database.") from exc
    if manifest.get("world_id") != WORLD_ID:
        raise ValueError("This is not the requested lighthouse world; check database and universe.")
    return db


def initialize_local(db: Path, private_dir: Path, universe: str = UNIVERSE) -> dict:
    """Create synthetic participants and one bounded referee, never reset a world."""
    from examples.dnd_world import make_world

    db, private_dir = _path(db), _path(private_dir)
    if db.exists():
        raise ValueError("Initialization requires a new database; existing worlds are never reset.")
    if any((p / ".git").exists() for p in (private_dir, *private_dir.parents)):
        raise ValueError("Keep identity keys outside Git working trees.")
    for directory in (db.parent, private_dir):
        if directory.exists():
            _private_directory(directory)
    if private_dir.exists() and any(private_dir.iterdir()):
        raise ValueError("Initialization requires an empty private identity directory.")
    if make_world().world_id != WORLD_ID:
        raise ValueError("Unexpected world package identity.")
    previous = os.umask(0o077)
    runtime = None
    try:
        db.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        private_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        runtime = WorldRuntime(db)
        participants = {}
        for label, display in (("A", "Local explorer A"), ("B", "Local explorer B"),
                               ("C", "Local recorder C"), ("GM", "Local bounded referee")):
            folder = private_dir / label
            folder.mkdir(mode=0o700)
            key_path = folder / "identity.json"
            key = load_identity(key_path, new_identity=True)
            challenge = runtime.issue_identity_key_challenge(universe, key.public_key)
            message = base64.urlsafe_b64decode(challenge["message"] + "=" * (-len(challenge["message"]) % 4))
            signature = base64.urlsafe_b64encode(key.private.sign(message)).rstrip(b"=").decode("ascii")
            joined = runtime.exchange_identity_key_challenge(challenge["challenge_id"], signature, expected_universe=universe)
            role_id = joined["role_profile"]["role_id"]
            runtime.update_role_profile(role_id, display_name=display)
            runtime.revoke_identity_token(joined["identity"]["token_id"])
            participants[label] = {"role_id": role_id, "key_path": str(key_path)}
        install_world(runtime, universe, make_world(judge_role_ids=(participants["GM"]["role_id"],)))
        result = {"database": str(db), "universe": universe, "world_profile": PROFILE,
                  "world_id": WORLD_ID, "participants": participants}
        with (private_dir / "setup.json").open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(db, 0o600)
        return result
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            os.umask(previous)


def create_dnd_application(db: Path, *, universe: str = UNIVERSE, port: int = 8796):
    db = existing_world(db, universe)
    inner = create_application(db, world_profile=PROFILE, universe=universe,
                               public_base_url=f"http://127.0.0.1:{port}")
    try:
        web = files("examples").joinpath("dnd_web")

        async def home(request):
            return FileResponse(web.joinpath("index.html"), headers={"Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

        @asynccontextmanager
        async def lifespan(app):
            async with inner.router.lifespan_context(inner):
                yield

        app = Starlette(routes=[Route("/", home), Mount("/dnd-static", StaticFiles(directory=str(web))),
                               Mount("/", inner)], lifespan=lifespan)
        app.state.runtime = inner.state.runtime
        return app
    except BaseException:
        inner.state.runtime.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Run an isolated classic fifth-edition lighthouse example.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--universe", default=UNIVERSE)
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--init-local", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    os.umask(0o077)
    try:
        if args.init_local:
            if args.private_dir is None:
                parser.error("--init-local requires --private-dir outside Git")
            initialize_local(args.db, args.private_dir, args.universe)
            print("Local world provisioned. Recovery keys are in the requested private directory.")
        else:
            existing_world(args.db, args.universe)
        import uvicorn
        uvicorn.run(create_dnd_application(args.db, universe=args.universe, port=args.port),
                    host="127.0.0.1", port=args.port)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
