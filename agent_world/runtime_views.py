from __future__ import annotations

import hashlib
import json
import secrets
import time

from .runtime_contracts import identifier, arguments_object, json_text, validate, integer
from .runtime_errors import PermissionDenied, InvalidArguments
from .world_views import ViewNotFound, ViewResetRequired, diff_view, MAX_VIEW_BYTES
from .presentation import PRESENTATION_KIND

VIEW_TTL_SECONDS = 300
VIEW_CACHE_PER_VIEWER = 16
VIEW_CACHE_MAX = 1024


class RuntimeViews:
    def _admit_viewer_tx(self, c, universe, role_id, identity_token):
        # Public observers are not synthetic players or control credentials.
        if role_id is None:
            if identity_token is not None:
                raise PermissionDenied("anonymous observation cannot borrow a role credential")
            return None
        return self._admit_actor_tx(c, universe, role_id, identity_token)

    def public_view_snapshot(self, universe, view, arguments=None):
        return self.view_snapshot(universe, None, view, arguments)

    def public_view_sync(self, universe, cursor):
        return self.view_sync(universe, None, cursor)

    def _init_views_tx(self, c):
        c.execute("""CREATE TABLE IF NOT EXISTS view_checkpoints(
            cursor_hash TEXT PRIMARY KEY, universe TEXT NOT NULL, role_id TEXT NOT NULL,
            credential_id TEXT NOT NULL, view_name TEXT NOT NULL, selector_json TEXT NOT NULL,
            world_version INTEGER NOT NULL, view_version INTEGER NOT NULL,
            body_json TEXT NOT NULL, state_revision INTEGER NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
        if "event_seq" not in {r["name"] for r in c.execute("PRAGMA table_info(view_checkpoints)")}:
            c.execute("ALTER TABLE view_checkpoints ADD COLUMN event_seq INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_view_checkpoint_expiry ON view_checkpoints(expires_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_view_checkpoint_owner ON view_checkpoints(universe,role_id,created_at)")

    @staticmethod
    def _view_cursor_hash(cursor):
        identifier(cursor, "view cursor", 128)
        if not cursor.startswith("awv_"):
            raise ViewResetRequired("unknown observation checkpoint; take a new snapshot")
        return hashlib.sha256(cursor.encode()).hexdigest()

    def _get_view_spec(self, universe, name):
        definition = self._worlds.get(universe)
        if definition is None:
            raise ViewNotFound("world does not declare observation views")
        for spec in definition.views:
            if spec.name == name:
                return definition, spec
        raise ViewNotFound("unknown observation view")

    def list_views(self, universe, role_id, *, identity_token=None, after="", limit=50, include_schemas=False):
        integer(limit, "limit", 1, 64)
        if not isinstance(after, str) or len(after) > 128 or type(include_schemas) is not bool:
            raise InvalidArguments("invalid view discovery parameters")
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            self._admit_viewer_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            definition = self._worlds.get(universe)
            items = []
            for spec in sorted(definition.views if definition else (), key=lambda item: item.name):
                if role_id is None and not spec.public:
                    continue
                if spec.name <= after:
                    continue
                ctx = self._context_tx(c, universe, role_id, "view:" + spec.name, spec.version)
                if spec.visible_to is not None and self._run_user_code(c, spec.visible_to, ctx, read_only=True) is not True:
                    continue
                item = spec.contract()
                if not include_schemas:
                    item = {k: item[k] for k in ("name", "version", "description", "renderer")}
                items.append(item)
            more = len(items) > limit
            result = {"views": items[:limit], "has_more": more,
                      "next_cursor": items[limit-1]["name"] if more else None}
            json_text(result)
            return result

    def view_snapshot(self, universe, role_id, view, arguments=None, *, identity_token=None):
        return self._observe_view(universe, role_id, view, {} if arguments is None else arguments,
                                  identity_token=identity_token)

    def view_sync(self, universe, role_id, cursor, *, identity_token=None):
        return self._observe_view(universe, role_id, None, None, identity_token=identity_token, cursor=cursor)

    def view_timeline(self, universe, role_id, cursor, *, limit=50, identity_token=None):
        """Ordered public cues for the authorized, currently visible subjects of one view."""
        import hmac
        integer(limit, "timeline limit", 1, 100)
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            identity = self._admit_actor_tx(c, universe, role_id, identity_token)
            credential = identity["token_id"] if identity else "trusted:" + role_id
            self._check_world_tx(c, universe)
            previous = c.execute("SELECT * FROM view_checkpoints WHERE cursor_hash=?",
                                 (self._view_cursor_hash(cursor),)).fetchone()
            if (previous is None or previous["universe"] != universe or previous["role_id"] != role_id
                    or previous["credential_id"] != credential or previous["expires_at"] <= time.time()
                    or previous["event_seq"] is None):
                raise ViewResetRequired("timeline checkpoint unavailable; reset the view")
            definition = self._worlds.get(universe)
            if definition is None or definition.version != previous["world_version"]:
                raise ViewResetRequired("world changed; reset the view")
            definition, spec = self._get_view_spec(universe, previous["view_name"])
            if not spec.timeline or spec.version != previous["view_version"]:
                raise ViewResetRequired("view does not provide this timeline")
            ctx = self._context_tx(c, universe, role_id, "view:" + spec.name, spec.version)
            args = json.loads(previous["selector_json"])
            if spec.visible_to is not None and self._run_user_code(c, spec.visible_to, ctx, read_only=True) is not True:
                raise PermissionDenied("view is no longer visible; clear it")
            if spec.authorize is not None and self._run_user_code(c, spec.authorize, ctx, args, read_only=True) is not True:
                raise PermissionDenied("view access denied; clear it")
            body = spec.validate_result(self._run_user_code(c, spec.project, ctx,
                                         json.loads(previous["selector_json"]), read_only=True))
            floor_row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
            floor = floor_row[0] if floor_row else 0
            if previous["event_seq"] < floor:
                raise ViewResetRequired("timeline was pruned; recover current state without replaying missing cues")
            latest = max(floor, c.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=?", (universe,)).fetchone()[0])
            rows = c.execute("SELECT * FROM events WHERE universe=? AND recipient_role_id=? AND kind=? "
                             "AND seq>? ORDER BY seq LIMIT ?",
                             (universe, role_id, PRESENTATION_KIND, previous["event_seq"], limit + 1)).fetchall()
            events, scanned, used = [], [], 0
            for row in rows[:limit]:
                cue = json.loads(row["payload_json"])
                if cue["subject_id"] in body["entities"]:
                    event = {"event_id": hmac.new(self._identity_secret(c),
                             json_text([universe, role_id, row["seq"]]).encode(), hashlib.sha256).hexdigest(),
                             "occurred_at": row["created_at"], "actor_role_id": row["actor_role_id"], "cue": cue}
                    size = len(json_text(event).encode())
                    if used + size > 196608:
                        break
                    events.append(event)
                    used += size
                scanned.append(row["seq"])
            more = len(scanned) < len(rows)
            next_seq = scanned[-1] if more and scanned else latest
            self._admit_actor_tx(c, universe, role_id, identity_token)
        next_cursor = "awv_" + secrets.token_urlsafe(32)
        now = time.time()
        result = {"view": spec.name, "viewer_role_id": role_id, "world_version": definition.version,
                  "view_version": spec.version, "base_cursor": cursor, "cursor": next_cursor,
                  "events": events, "has_more": more, "expires_at": now + VIEW_TTL_SECONDS}
        json_text(result)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            c.execute("DELETE FROM view_checkpoints WHERE expires_at<=?", (now,))
            c.execute("INSERT INTO view_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self._view_cursor_hash(next_cursor), universe, role_id, credential, spec.name,
                       previous["selector_json"], definition.version, spec.version, previous["body_json"],
                       previous["state_revision"], now, now + VIEW_TTL_SECONDS, next_seq))
            for clause, params, maximum in (
                ("universe=? AND role_id=?", (universe, role_id), VIEW_CACHE_PER_VIEWER), ("1=1", (), VIEW_CACHE_MAX)
            ):
                c.execute("DELETE FROM view_checkpoints WHERE cursor_hash IN (SELECT cursor_hash FROM view_checkpoints WHERE "
                          + clause + " ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (*params, maximum))
        return result

    def _observe_view(self, universe, role_id, view, arguments, *, identity_token, cursor=None):
        # World reads use one read snapshot. Only the bounded derived checkpoint is written afterwards.
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            identity = self._admit_viewer_tx(c, universe, role_id, identity_token)
            credential = identity["token_id"] if identity else ("public" if role_id is None else "trusted:" + role_id)
            self._check_world_tx(c, universe)
            previous = None
            if cursor is not None:
                digest = self._view_cursor_hash(cursor)
                previous = c.execute("SELECT * FROM view_checkpoints WHERE cursor_hash=?", (digest,)).fetchone()
                if (previous is None or previous["universe"] != universe or previous["role_id"] != (role_id or "")
                        or previous["credential_id"] != credential or previous["expires_at"] <= time.time()):
                    raise ViewResetRequired("observation checkpoint expired or has a different viewer; reset the view")
                view, arguments = previous["view_name"], json.loads(previous["selector_json"])
            identifier(view, "view name")
            arguments_object(arguments)
            if previous is not None:
                loaded = self._worlds.get(universe)
                if loaded is None or loaded.version != previous["world_version"]:
                    raise ViewResetRequired("world projection changed; take a new snapshot")
            definition, spec = self._get_view_spec(universe, view)
            if role_id is None and not spec.public:
                raise PermissionDenied("view is not public")
            if previous is not None and (previous["world_version"] != definition.version or previous["view_version"] != spec.version):
                raise ViewResetRequired("view contract changed; take a new snapshot")
            validate(spec.input_schema, arguments)
            selector_json = json_text(arguments)
            ctx = self._context_tx(c, universe, role_id, "view:" + view, spec.version)
            if spec.visible_to is not None and self._run_user_code(c, spec.visible_to, ctx, read_only=True) is not True:
                raise PermissionDenied("view is not available to this viewer; clear it")
            if spec.authorize is not None and self._run_user_code(c, spec.authorize, ctx, json.loads(selector_json), read_only=True) is not True:
                raise PermissionDenied("view access denied; clear it")
            body = spec.validate_result(self._run_user_code(c, spec.project, ctx, json.loads(selector_json), read_only=True))
            encoded = json_text(body, maximum=MAX_VIEW_BYTES)
            body = json.loads(encoded)
            revision, _ = self._state_revision_tx(c, universe)
            event_floor_row = c.execute("SELECT floor FROM event_floors WHERE universe=?", (universe,)).fetchone()
            event_seq = max(event_floor_row[0] if event_floor_row else 0, c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE universe=?", (universe,)).fetchone()[0])
            streams = {}
            for name in spec.streams:
                try:
                    streams[name] = self._stream_anchor_tx(c,universe,name,role_id,identity_token)
                except PermissionDenied:
                    continue
            observed_at = time.time()
            self._admit_viewer_tx(c, universe, role_id, identity_token)
            base = json.loads(previous["body_json"]) if previous is not None else None
        # Recompute and authorize above on every read. Only reuse the derived
        # checkpoint, never a stale projection or a credential decision. Timeline
        # checkpoints still advance their event anchor via the existing path.
        reuse = (previous is not None and not spec.timeline
                 and previous["body_json"] == encoded
                 and previous["expires_at"] - observed_at > 30)
        next_cursor = cursor if reuse else "awv_" + secrets.token_urlsafe(32)
        expires = previous["expires_at"] if reuse else observed_at + VIEW_TTL_SECONDS
        result = {"view": view, "world_version": definition.version, "view_version": spec.version,
                  "renderer": spec.renderer, "viewer_role_id": role_id,
                  "view_revision": hashlib.sha256(encoded.encode()).hexdigest(),
                  "observed_at": observed_at, "expires_at": expires, "cursor": next_cursor}
        if spec.timeline:
            result["timeline_cursor"] = next_cursor
        if streams:
            result["streams"] = streams
        if base is None:
            result.update(kind="snapshot", snapshot=body)
        else:
            result.update(kind="delta", base_cursor=cursor, delta=diff_view(base, body))
        json_text(result)
        if reuse:
            return result
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_viewer_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            c.execute("DELETE FROM view_checkpoints WHERE expires_at<=?", (time.time(),))
            c.execute("""INSERT INTO view_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (self._view_cursor_hash(next_cursor), universe, role_id or "", credential, view,
                       selector_json, definition.version, spec.version, encoded, revision, observed_at, expires, event_seq))
            # Keep the newly returned checkpoint, evict only older derived cache entries.
            for clause, params, maximum in (
                ("universe=? AND role_id=?", (universe, role_id or ""), 256 if role_id is None else VIEW_CACHE_PER_VIEWER),
                ("1=1", (), VIEW_CACHE_MAX),
            ):
                c.execute("DELETE FROM view_checkpoints WHERE cursor_hash IN (SELECT cursor_hash FROM view_checkpoints WHERE "
                          + clause + " ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (*params, maximum))
        return result
