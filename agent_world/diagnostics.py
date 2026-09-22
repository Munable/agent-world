"""Opt-in, bounded request diagnostics. Never log bodies, credentials, cookies or raw URLs."""
from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid

class SafeRequestTrace:
    def __init__(self, app, *, path=None, functions=(), sink=None):
        self.app=app; self.functions=frozenset(functions); self.sink=sink
        self.logger=None
        if path is not None:
            path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
            self.logger=logging.Logger('world-request-trace.'+uuid.uuid4().hex)
            handler=RotatingFileHandler(path,maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(handler)
    def _route(self, path):
        if path=='/mcp' or path.startswith('/mcp/'):
            return '/mcp'
        if path.startswith('/v1/functions/'):
            parts=path.split('/')
            name=parts[3] if len(parts)>3 else ''
            return '/v1/functions/'+(name if name in self.functions else '{unknown}')+'/invoke'
        for prefix in ('/v1/streams/','/v1/public/streams/','/v1/views/','/v1/public/views/','/v1/receipts/','/static/','/bridge/'):
            if path.startswith(prefix):return prefix+'{resource}'
        if path in {'/','/watch','/health','/play/session','/play/sync','/play/join','/play/action','/play/agent','/play/history',
                    '/play/logout','/play/map','/watch/session','/watch/sync','/watch/history','/v1/join/exchange',
                    '/v1/functions','/v1/discover','/v1/changes','/v1/changes/wait','/v1/bootstrap','/v1/whoami'}:
            return path
        return '/{other}'
    async def __call__(self, scope, receive, send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        rid=uuid.uuid4().hex; started=time.monotonic(); status=500; error=None
        async def traced_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status']; message={**message,'headers':list(message.get('headers',[]))+[(b'x-request-id',rid.encode())]}
            await send(message)
        try:
            return await self.app(scope,receive,traced_send)
        except BaseException as exc:
            error=type(exc).__name__
            raise
        finally:
            record={'request_id':rid,'at':time.time(),'method':scope.get('method') if scope.get('method') in ('GET','POST','PUT','DELETE','PATCH','HEAD','OPTIONS') else 'OTHER',
                    'route':self._route(scope.get('path','')),'http_status':status,
                    'duration_ms':round((time.monotonic()-started)*1000,2),'exception_type':error}
            # Observability failure must not change the result of a committed world action.
            try:
                if self.sink is not None:self.sink(record)
                if self.logger is not None:self.logger.info(json.dumps(record,separators=(',',':')))
            except Exception:
                pass
