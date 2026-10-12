"""Provision and serve an isolated, loopback-only participant decision example."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat

from starlette.responses import HTMLResponse
from starlette.routing import Route

from agent_world import WorldRuntime
from agent_world.application import create_application
from agent_world.connect import load_identity
from agent_world.world_sdk import install_world

WORLD_ID = "participant-decisions"
UNIVERSE = "participant-decisions"
PROFILE = "examples.participant_world:WORLD"


def _path(value: Path) -> Path:
    value = value.expanduser().absolute()
    if ".." in value.parts or any(p.is_symlink() for p in (value, *value.parents)):
        raise ValueError("Use normalized real paths without symbolic links for local world data.")
    return value


def _private_directory(value: Path) -> None:
    info = value.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("World data directories must be owned by you and have permissions 0700.")


def existing_world(db: Path, universe: str = UNIVERSE) -> Path:
    """Check an existing database without creating, replacing, or migrating it."""
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
    if not isinstance(manifest, dict) or manifest.get("world_id") != WORLD_ID:
        raise ValueError("This is not the requested participant decision world; check database and universe.")
    return db


def initialize_local(db: Path, private_dir: Path, universe: str = UNIVERSE) -> dict:
    """Create three synthetic key identities and a new world; never reset one."""
    from examples.participant_world import make_world

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
    world = make_world()
    if world.world_id != WORLD_ID:
        raise ValueError("Unexpected world package identity.")
    previous = os.umask(0o077)
    runtime = None
    try:
        db.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        private_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        _private_directory(db.parent)
        _private_directory(private_dir)
        # Reserve the path exclusively before Runtime opens SQLite. A concurrent
        # initializer must fail instead of opening somebody else's new database.
        with db.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
        runtime = WorldRuntime(db)
        participants = {}
        for label in ("A", "B", "C"):
            folder = private_dir / label
            folder.mkdir(mode=0o700)
            key_path = folder / "identity.json"
            key = load_identity(key_path, new_identity=True)
            challenge = runtime.issue_identity_key_challenge(universe, key.public_key)
            message = base64.urlsafe_b64decode(challenge["message"] + "=" * (-len(challenge["message"]) % 4))
            signature = base64.urlsafe_b64encode(key.private.sign(message)).rstrip(b"=").decode("ascii")
            joined = runtime.exchange_identity_key_challenge(challenge["challenge_id"], signature, expected_universe=universe)
            role_id = joined["role_profile"]["role_id"]
            runtime.update_role_profile(role_id, display_name=f"Local participant {label}")
            runtime.revoke_identity_token(joined["identity"]["token_id"])
            participants[label] = {"role_id": role_id, "key_path": str(key_path)}
        install_world(runtime, universe, world)
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


def create_participant_application(db: Path, *, universe: str = UNIVERSE, port: int = 8802):
    db = existing_world(db, universe)
    app = create_application(db, world_profile=PROFILE, universe=universe,
                             public_base_url=f"http://127.0.0.1:{port}", timers_enabled=False)

    async def home(request):
        return HTMLResponse("""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>参与式裁决本地示例</title>
<body><main><h1>参与式裁决本地示例</h1>
<p>此隔离实例用于验证正式身份参与、裁决和结果接续。通过 HTTP 或 MCP 参与；本页仅说明接口用途。</p>
<p>不自动超时或改派；没有后台模型或自动裁判。当前规则与获准状态以入场和函数发现接口为准。</p>
<ul>
<li><code>GET /health</code>：检查服务。</li>
<li><code>POST /v1/key-identities/challenges</code> 与 <code>POST /v1/key-identities/exchange</code>：持钥签名入场。</li>
<li><code>GET /v1/bootstrap</code>：读取本人档案、世界说明和获准入场状态。</li>
<li><code>GET /v1/discover?include_schemas=true</code>：发现可用行动及参数。</li>
<li><code>POST /v1/functions/{function_id}/invoke</code>：调用世界查询或提交行动。</li>
<li><code>GET /v1/receipts/{operation_id}</code>：恢复本人已提交的行动结果。</li>
<li><code>GET /v1/changes</code>：读取本人获准通知并保存续接游标。</li>
<li><code>/mcp</code>：使用同一正式凭据连接 MCP。</li>
</ul><p>除健康检查和持钥入场外，世界接口需要正式凭据。写行动使用稳定的 operation_id；未知结果先查询原回执。</p>
</main></body></html>""", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                   "Referrer-Policy": "no-referrer"})

    app.router.routes.insert(0, Route("/", home))
    return app


def main():
    parser = argparse.ArgumentParser(description="Run an isolated participant decision example on loopback.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--init-local", action="store_true")
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    os.umask(0o077)
    try:
        if args.init_local:
            if args.private_dir is None:
                parser.error("--init-local requires --private-dir outside Git")
            initialize_local(args.db, args.private_dir)
            print("Local world provisioned. Recovery keys are in the requested private directory.")
        else:
            existing_world(args.db)
        import uvicorn
        uvicorn.run(create_participant_application(args.db, port=args.port), host="127.0.0.1", port=args.port)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
