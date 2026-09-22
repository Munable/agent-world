from __future__ import annotations

import threading


class LazyASGI:
    """Preserve uvicorn module:app entry points without database writes on import."""

    def __init__(self, factory):
        self.factory = factory
        self._app = None
        self._lock = threading.Lock()

    def _resolve(self):
        with self._lock:
            if self._app is None:
                self._app = self.factory()
        return self._app

    async def __call__(self, scope, receive, send):
        return await self._resolve()(scope, receive, send)
