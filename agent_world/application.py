from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import argparse
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount

from .http_app import create_app as create_http_app
from .mcp_app import create_mcp_app, BearerIdentityMiddleware
from .product_app import create_product_app
from .universe_loader import get_universe_installer
from .timer_worker import timer_lifespan


def create_application(
    db_path,
    *,
    world_profile="world-zero",
    universe="world-zero",
    public_base_url="http://127.0.0.1:8000",
    web_user="operator",
    web_password=None,
    timers_enabled=True,
    timer_interval=1.0,
):
    parsed = urlsplit(public_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("public_base_url must be a credential-free absolute HTTP(S) origin")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("non-loopback deployments require an HTTPS public origin")
    public_base_url = public_base_url.rstrip("/")
    db_path = Path(db_path).expanduser().resolve()
    installer = get_universe_installer(world_profile)
    mcp_app, _, mcp_runtime = create_mcp_app(
        db_path, universe, auth_required=True, installer=installer, host=parsed.hostname
    )
    http_app = create_http_app(db_path, universe, auth_required=True, installer=installer)
    web_app = create_product_app(
        db_path,
        universe,
        mcp_url=public_base_url + "/mcp",
        public_base_url=public_base_url,
        web_user=web_user,
        web_password=web_password,
    )
    mcp_base = mcp_app.app if isinstance(mcp_app, BearerIdentityMiddleware) else mcp_app

    class Dispatch:
        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                target = mcp_app
            elif path == "/" or path.startswith(("/api/", "/static/")) or path == "/v1/join/exchange":
                target = web_app
            else:
                target = http_app
            await target(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_base.router.lifespan_context(mcp_base):
            async with timer_lifespan(mcp_runtime, universe, enabled=timers_enabled, interval=timer_interval):
                yield

    app = Starlette(routes=[Mount("/", app=Dispatch())], lifespan=lifespan)
    app.state.runtime = mcp_runtime
    return app


def main():
    parser = argparse.ArgumentParser(description="Run one authenticated Agent World endpoint.")
    parser.add_argument(
        "--world", default="world-zero", help="built-in name or trusted installed module:WORLD"
    )
    parser.add_argument("--universe", default=None)
    parser.add_argument("--db", default="agent-world.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--public-url", default=None)
    parser.add_argument("--no-timers", action="store_true", help="disable the embedded timer worker")
    parser.add_argument("--timer-interval", type=float, default=1.0)
    args = parser.parse_args()
    universe = args.universe or (args.world if ":" not in args.world else "custom")
    app = create_application(
        args.db,
        world_profile=args.world,
        universe=universe,
        public_base_url=args.public_url or f"http://127.0.0.1:{args.port}",
        web_user=os.getenv("WORLD_WEB_USER", "operator"),
        web_password=os.getenv("WORLD_WEB_PASSWORD"),
        timers_enabled=not args.no_timers,
        timer_interval=args.timer_interval,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
