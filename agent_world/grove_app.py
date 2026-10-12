"""Compose the official spatial world with the existing authenticated Runtime.

The world server runs ordinary rules and timers. Personal OpenCode sessions live
in a separate, loopback connector; this server never runs inference.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager, closing
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
import stat

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .application import create_application
from .runtime_core import WorldRuntime
from .world_sdk import install_world

WORLD_ID = UNIVERSE = "shared-grove"
PROFILE = "agent_world.worlds.shared_grove:WORLD"


def checked_path(value: Path) -> Path:
    path = Path(value).expanduser().absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Use a real, normalized database path without symbolic links.")
    if any((p / ".git").exists() for p in (path.parent, *path.parent.parents)):
        raise ValueError("Keep live world data outside Git working trees.")
    return path


def private_directory(directory: Path) -> None:
    info = directory.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("World data directories must be privately owned with permissions 0700.")


def existing_world(db: Path, universe: str = UNIVERSE) -> Path:
    db = checked_path(db)
    if not db.exists():
        raise ValueError("The world database is missing; initialize a new database with --init.")
    private_directory(db.parent)
    info = db.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("Use a regular, privately owned database with permissions 0600.")
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM world_definitions WHERE universe=?", (universe,)
            ).fetchone()
        manifest = json.loads(row[0]) if row else {}
    except (ValueError, sqlite3.DatabaseError) as exc:
        raise ValueError("The file is not a readable Agent World database.") from exc
    if not isinstance(manifest, dict) or manifest.get("world_id") != WORLD_ID:
        raise ValueError("This is not a Shared Grove instance; preserve the existing database.")
    return db


def initialize_local(db: Path, universe: str = UNIVERSE) -> dict:
    """Provision one new spatial world without inventing user or Agent identities."""
    from .worlds.shared_grove import make_world

    db = checked_path(db)
    if db.exists():
        raise ValueError("Initialization requires a new database and never resets an existing world.")
    if db.parent.exists():
        private_directory(db.parent)
    previous = os.umask(0o077)
    try:
        db.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        private_directory(db.parent)
        with db.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
        with closing(WorldRuntime(db)) as runtime:
            install_world(runtime, universe, make_world())
        os.chmod(db, 0o600)
        return {"database": str(db), "universe": universe, "world_id": WORLD_ID,
                "world_profile": PROFILE}
    finally:
        os.umask(previous)


def create_grove_application(db: Path, *, universe: str = UNIVERSE, port: int = 8890,
                             timers_enabled: bool = True):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    db = existing_world(db, universe)
    inner = create_application(db, world_profile=PROFILE, universe=universe,
                               public_base_url=f"http://127.0.0.1:{port}",
                               timers_enabled=timers_enabled, timer_interval=0.2)
    try:
        web = files("agent_world").joinpath("grove_web")

        async def home(request):
            return HTMLResponse(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>溪源 · 共享世界</title><body><main><h1>溪源已启动</h1>'
                '<p>通过自己的本机 OpenCode 连接器进入，亲自探索、试验和改变同一个世界。</p>'
                '<p><a href="http://127.0.0.1:8891/">打开本机游玩入口</a></p>'
                '<p>世界会保存位置、物件和变化。个人助手的模型与记忆留在你的工具里。</p>'
                '</main></body></html>',
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                         "Referrer-Policy": "no-referrer"},
            )

        @asynccontextmanager
        async def lifespan(app):
            async with inner.router.lifespan_context(inner):
                yield

        app = Starlette(routes=[Route("/", home),
                                Mount("/grove-static", StaticFiles(directory=str(web))),
                                Mount("/", inner)], lifespan=lifespan)
        app.state.runtime = inner.state.runtime
        app.state.universe = universe
        return app
    except BaseException:
        inner.state.runtime.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Run the Shared Grove state/rules server on loopback.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--init", action="store_true", help="create a new world; never reset an existing one")
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    os.umask(0o077)
    try:
        if args.init:
            initialize_local(args.db)
        else:
            existing_world(args.db)
        import uvicorn
        uvicorn.run(create_grove_application(args.db, port=args.port), host="127.0.0.1", port=args.port)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
