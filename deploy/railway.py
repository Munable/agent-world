"""Single-instance Commons entrypoint behind Railway's HTTPS edge.

Deployment-only policy: no Runtime or world contract changes. Importing this
module does not create a database, change permissions, or start the server.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

DATA_ROOT = Path('/data')
RUN_UID = RUN_GID = 10001


def configuration(environ):
    if environ.get('WORLD_WEB_PASSWORD') or environ.get('WORLD_OPERATOR_KEY'):
        raise ValueError('operator credentials are not supported by this public Commons entrypoint')
    if environ.get('RAILWAY_VOLUME_MOUNT_PATH') != str(DATA_ROOT):
        raise ValueError('attach a persistent Railway volume at /data before starting')
    port = int(environ.get('PORT', '8080'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535')
    origin = environ.get('WORLD_PUBLIC_URL')
    if not origin and environ.get('RAILWAY_PUBLIC_DOMAIN'):
        origin = 'https://' + environ['RAILWAY_PUBLIC_DOMAIN']
    parsed = urlsplit(origin or '')
    host = parsed.hostname or ''
    if (
        parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
        or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
        or parsed.port not in {None, 443}
        or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
        or '.' not in host or '..' in host or host.endswith('.localhost')
        or re.fullmatch(r'[0-9.]+', host)
        or any(character.isspace() for character in (origin or ''))
    ):
        raise ValueError('set WORLD_PUBLIC_URL or RAILWAY_PUBLIC_DOMAIN to a public HTTPS origin')
    return 'https://' + host, port


def prepare_volume():
    if not DATA_ROOT.is_mount():
        raise ValueError('/data must be an actual mounted persistent volume; refusing ephemeral storage')
    os.umask(0o077)
    directory = DATA_ROOT / 'commons'
    if directory.is_symlink():
        raise ValueError('the Commons data directory must not be a symlink')
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    if os.geteuid() == 0:
        os.chown(directory, RUN_UID, RUN_GID)
    for name in ('commons.sqlite3', 'commons.sqlite3-wal', 'commons.sqlite3-shm'):
        path = directory / name
        if path.is_symlink():
            raise ValueError('SQLite storage files must not be symlinks')
        if path.exists():
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('SQLite storage must use regular files')
            path.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(path, RUN_UID, RUN_GID)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(RUN_GID)
        os.setuid(RUN_UID)
    return directory / 'commons.sqlite3'


class CommonsPublicSurface:
    """Expose only Commons UI/host routes, with a fixed public origin.

Railway terminates TLS. No client-supplied forwarding header is trusted: the
origin is deployment configuration, and every non-health request must match it.
This also protects external-host MCP, for which the SDK has no default allowlist.
"""

    def __init__(self, app, origin):
        self.app = app
        self.origin = origin
        self.host = urlsplit(origin).netloc

    @staticmethod
    def allowed(path, method):
        if path == '/mcp':
            return method in {'GET', 'POST', 'DELETE'}
        if path in {'/v1/key-identities/challenges', '/v1/key-identities/exchange'}:
            return method == 'POST'
        if path == '/api/me':
            return method in {'GET', 'PATCH'}
        if path in {'/', '/health', '/api/config', '/v1/bootstrap', '/v1/whoami'}:
            return method in {'GET', 'HEAD'}
        if path in {'/static/commons.css', '/static/commons.js', '/static/commons-client.js'}:
            return method in {'GET', 'HEAD'}
        if re.fullmatch(r'/v1/functions/[A-Za-z0-9_.-]+/invoke', path):
            return method == 'POST'
        return method == 'GET' and re.fullmatch(r'/v1/receipts/[^/]+', path) is not None

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from starlette.responses import PlainTextResponse

        path, method = scope.get('path', ''), scope.get('method', '')
        if not self.allowed(path, method):
            return await PlainTextResponse('Not Found', status_code=404)(scope, receive, send)
        # Railway's startup probe uses an internal Host. This exception exposes
        # only the existing read-only liveness response, never the application.
        if path != '/health':
            headers = scope.get('headers', [])
            hosts = [value.decode('latin1') for key, value in headers if key.lower() == b'host']
            origins = [value.decode('latin1') for key, value in headers if key.lower() == b'origin']
            if len(hosts) != 1 or hosts[0].lower() not in {self.host, self.host + ':443'}:
                return await PlainTextResponse('Invalid Host', status_code=421)(scope, receive, send)
            if len(origins) > 1 or (origins and origins[0] != self.origin):
                return await PlainTextResponse('Invalid Origin', status_code=403)(scope, receive, send)
            # Canonicalize accepted default-port Host spelling so the existing
            # web-safety comparison sees the same origin as the browser.
            scope = {**scope, 'scheme': 'https', 'headers': [
                (key, self.host.encode('ascii') if key.lower() == b'host' else value)
                for key, value in headers
            ]}
        return await self.app(scope, receive, send)


def main():
    origin, port = configuration(os.environ)
    db = prepare_volume()
    from agent_world.application import create_application
    import uvicorn

    app = create_application(
        db, world_profile='commons', universe='commons', public_base_url=origin,
        web_password=None,
    )
    uvicorn.run(
        CommonsPublicSurface(app, origin), host='0.0.0.0', port=port, workers=1,
        proxy_headers=False, timeout_graceful_shutdown=25,
    )


if __name__ == '__main__':
    main()
