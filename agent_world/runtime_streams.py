from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
from .runtime_contracts import identifier, integer, json_text, MAX_RESULT_BYTES
from .runtime_errors import PermissionDenied, InvalidArguments, ResultRejected
from .world_streams import StreamEvent, StreamResetRequired, StreamNotFound, event_id

CURSOR_LIFETIME = 86400
PAGE_BYTES = 196608

class RuntimeStreams:
    def _init_streams_tx(self, c):
        c.execute("""CREATE TABLE IF NOT EXISTS stream_heads(
            universe TEXT NOT NULL, stream TEXT NOT NULL, head INTEGER NOT NULL DEFAULT 0,
            floor INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(universe,stream))""")
        c.execute("""CREATE TABLE IF NOT EXISTS stream_events(
            universe TEXT NOT NULL, stream TEXT NOT NULL, seq INTEGER NOT NULL,
            event_id TEXT NOT NULL, actor_role_id TEXT NOT NULL, kind TEXT NOT NULL,
            payload_json TEXT NOT NULL, created_at REAL NOT NULL, commit_seq INTEGER,
            PRIMARY KEY(universe,stream,seq), UNIQUE(universe,stream,event_id),
            FOREIGN KEY(commit_seq) REFERENCES world_commits(seq))""")

    def _stream_spec(self, universe, name):
        identifier(universe, 'universe'); identifier(name, 'stream', 64)
        definition = self._worlds.get(universe)
        if definition is not None:
            for spec in definition.streams:
                if spec.name == name:
                    return definition, spec
        raise StreamNotFound('stream not declared by this world')

    def _stream_admit_tx(self, c, universe, name, role_id, token):
        self._check_world_tx(c, universe)
        definition, spec = self._stream_spec(universe, name)
        if role_id is None:
            if token is not None or not spec.public:
                raise PermissionDenied('stream requires an authorized viewer')
            credential = 'public'
        else:
            identity = self._admit_actor_tx(c, universe, role_id, token)
            credential = identity['token_id'] if identity else 'trusted:' + role_id
        if spec.authorize is not None:
            ctx = self._context_tx(c, universe, role_id, 'stream:' + name, 1)
            if self._run_user_code(c, spec.authorize, ctx, read_only=True) is not True:
                raise PermissionDenied('stream is not visible to this viewer')
        return definition, spec, credential

    @staticmethod
    def _head_tx(c, universe, stream):
        row = c.execute('SELECT head,floor FROM stream_heads WHERE universe=? AND stream=?', (universe,stream)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _stream_cursor_tx(self, c, universe, stream, role, credential, version, seq, mode):
        body = json_text([1, universe, stream, role, credential, version, seq, mode, int(time.time()) + CURSOR_LIFETIME]).encode()
        encoded = base64.urlsafe_b64encode(body).rstrip(b'=').decode()
        signature = hmac.new(self._identity_secret(c), b'stream-v1\0' + body, hashlib.sha256).hexdigest()
        return 'aws_' + encoded + '.' + signature

    def _parse_stream_cursor_tx(self, c, cursor, universe, stream, role, credential, version):
        try:
            if not isinstance(cursor,str) or not cursor.startswith('aws_') or len(cursor)>2048:
                raise ValueError()
            encoded, signature = cursor[4:].split('.')
            body = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded)%4))
            expected = hmac.new(self._identity_secret(c), b'stream-v1\0' + body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            parts = json.loads(body)
            if len(parts)!=9 or parts[:6] != [1,universe,stream,role,credential,version]:
                raise ValueError()
            seq, mode, expires = parts[6:]
            integer(seq,'cursor position')
            if mode not in ('live','history') or expires <= time.time():
                raise ValueError()
            return seq, mode
        except Exception:
            raise StreamResetRequired('stream cursor expired or does not match viewer/world; reload recent history') from None

    def _stream_anchor_tx(self, c, universe, name, role_id, identity_token):
        definition, spec, credential = self._stream_admit_tx(c, universe, name, role_id, identity_token)
        head, floor = self._head_tx(c, universe, name)
        make = lambda seq, mode: self._stream_cursor_tx(c,universe,name,role_id,credential,definition.version,seq,mode)
        return {'cursor': make(head,'live'), 'history_cursor': make(head+1,'history'),
                'retention_seconds': spec.retention_seconds, 'has_older': head>floor}

    def list_streams(self, universe, role_id=None, *, identity_token=None):
        with self._lock, self._conn(readonly=True) as c:
            c.execute('BEGIN'); self._check_world_tx(c,universe)
            definition = self._worlds.get(universe)
            result=[]
            for spec in definition.streams if definition else ():
                try:
                    self._stream_admit_tx(c,universe,spec.name,role_id,identity_token)
                except PermissionDenied:
                    continue
                result.append(spec.contract())
            return {'streams':result}

    def read_stream(self, universe, name, role_id=None, *, identity_token=None, cursor=None, limit=50):
        integer(limit,'stream page size',1,100)
        with self._lock, self._conn(readonly=True) as c:
            c.execute('BEGIN')
            definition,spec,credential=self._stream_admit_tx(c,universe,name,role_id,identity_token)
            head,floor=self._head_tx(c,universe,name)
            seq,mode=(head+1,'recent') if cursor is None else self._parse_stream_cursor_tx(c,cursor,universe,name,role_id,credential,definition.version)
            if mode=='live' and (seq<floor or seq>head):
                raise StreamResetRequired('stream history no longer covers the cursor; reload recent history')
            reverse=mode!='live'
            op,order=('<','DESC') if reverse else ('>','ASC')
            rows=c.execute('SELECT * FROM stream_events WHERE universe=? AND stream=? AND seq '+op+'? ORDER BY seq '+order+' LIMIT ?',
                           (universe,name,seq,limit+1)).fetchall()
            picked=[]; size=0
            for row in rows[:limit]:
                item={'event_id':row['event_id'],'seq':row['seq'],'stream':name,
                      'actor_role_id':row['actor_role_id'],'kind':row['kind'],
                      'payload':json.loads(row['payload_json']),'occurred_at':row['created_at']}
                encoded=json_text(item)
                if picked and size+len(encoded.encode())>PAGE_BYTES:
                    break
                picked.append(item); size+=len(encoded.encode())
            more=len(picked)<len(rows)
            if reverse:
                picked.reverse()
            make=lambda pos,mode: self._stream_cursor_tx(c,universe,name,role_id,credential,definition.version,pos,mode)
            oldest=picked[0]['seq'] if picked else seq
            last=picked[-1]['seq'] if picked else seq
            next_seq=(last if more else head) if mode=='live' else head
            history_pos=oldest
            result={'stream':name,'mode':mode,'events':picked,
                    'cursor':make(next_seq,'live'),'history_cursor':make(history_pos,'history'),
                    'has_more':more if mode=='live' else False,
                    'has_older':more if reverse else history_pos>floor+1,
                    'history_truncated': bool(floor and reverse and not more),
                    'observed_at':time.time(), 'delivery':'returned_by_server_not_proof_of_reading'}
            self._stream_admit_tx(c,universe,name,role_id,identity_token)
            json_text(result)
            return result

    def _append_stream_event_tx(self,c,ctx,event,index):
        self._stream_spec(ctx.universe,event.stream)
        key=event.key or str(index)
        eid=event_id(ctx.universe,ctx.actor_role_id,ctx.function_id,ctx.operation_id,event.stream,key)
        c.execute('INSERT INTO stream_heads(universe,stream) VALUES(?,?) ON CONFLICT DO NOTHING',(ctx.universe,event.stream))
        c.execute('UPDATE stream_heads SET head=head+1 WHERE universe=? AND stream=?',(ctx.universe,event.stream))
        seq=c.execute('SELECT head FROM stream_heads WHERE universe=? AND stream=?',(ctx.universe,event.stream)).fetchone()[0]
        try:
            c.execute('INSERT INTO stream_events(universe,stream,seq,event_id,actor_role_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (ctx.universe,event.stream,seq,eid,ctx.actor_role_id,event.kind,json_text(event.payload),ctx.now))
        except Exception as exc:
            import sqlite3
            if isinstance(exc,sqlite3.IntegrityError):
                raise ResultRejected('duplicate publication key within one operation') from exc
            raise
        return event.stream,seq,eid

    def apply_stream_retention(self, universe, *, limit=500):
        integer(limit,'stream retention batch',1,5000)
        with self._lock,self._conn() as c:
            c.execute('BEGIN IMMEDIATE'); self._check_world_tx(c,universe)
            definition=self._worlds.get(universe)
            removed=0
            for spec in definition.streams if definition else ():
                count=c.execute('SELECT COUNT(*) FROM stream_events WHERE universe=? AND stream=?',(universe,spec.name)).fetchone()[0]
                excess=max(0,count-spec.max_events)
                rows=c.execute('SELECT seq,created_at FROM stream_events WHERE universe=? AND stream=? ORDER BY seq LIMIT ?',
                               (universe,spec.name,limit)).fetchall()
                through=None
                for i,row in enumerate(rows):
                    if i<excess or row['created_at']<=time.time()-spec.retention_seconds:
                        through=row['seq']
                    else:
                        break
                if through is not None:
                    removed+=c.execute('DELETE FROM stream_events WHERE universe=? AND stream=? AND seq<=?',(universe,spec.name,through)).rowcount
                    c.execute('UPDATE stream_heads SET floor=MAX(floor,?) WHERE universe=? AND stream=?',(through,universe,spec.name))
            return {'stream_events_removed':removed}
