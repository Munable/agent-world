from __future__ import annotations

import hashlib
import json
import secrets
import time

from .runtime_contracts import identifier, arguments_object, json_text, validate, integer
from .runtime_errors import PermissionDenied, InvalidArguments
from .world_views import ViewNotFound, ViewResetRequired, diff_view, MAX_VIEW_BYTES

VIEW_TTL_SECONDS = 300
VIEW_CACHE_PER_VIEWER = 16
VIEW_CACHE_MAX = 1024


class RuntimeViews:
    def _init_views_tx(self, c):
        c.execute("""CREATE TABLE IF NOT EXISTS view_checkpoints(
            cursor_hash TEXT PRIMARY KEY, universe TEXT NOT NULL, role_id TEXT NOT NULL,
            credential_id TEXT NOT NULL, view_name TEXT NOT NULL, selector_json TEXT NOT NULL,
            world_version INTEGER NOT NULL, view_version INTEGER NOT NULL,
            body_json TEXT NOT NULL, state_revision INTEGER NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
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
            self._admit_actor_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            definition = self._worlds.get(universe)
            items = []
            for spec in sorted(definition.views if definition else (), key=lambda item: item.name):
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

    def _observe_view(self, universe, role_id, view, arguments, *, identity_token, cursor=None):
        # World reads use one read snapshot. Only the bounded derived checkpoint is written afterwards.
        with self._lock, self._conn(readonly=True) as c:
            c.execute("BEGIN")
            identity = self._admit_actor_tx(c, universe, role_id, identity_token)
            credential = identity["token_id"] if identity else "trusted:" + role_id
            self._check_world_tx(c, universe)
            previous = None
            if cursor is not None:
                digest = self._view_cursor_hash(cursor)
                previous = c.execute("SELECT * FROM view_checkpoints WHERE cursor_hash=?", (digest,)).fetchone()
                if (previous is None or previous["universe"] != universe or previous["role_id"] != role_id
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
            observed_at = time.time()
            self._admit_actor_tx(c, universe, role_id, identity_token)
            base = json.loads(previous["body_json"]) if previous is not None else None
        next_cursor = "awv_" + secrets.token_urlsafe(32)
        expires = observed_at + VIEW_TTL_SECONDS
        result = {"view": view, "world_version": definition.version, "view_version": spec.version,
                  "renderer": spec.renderer, "viewer_role_id": role_id,
                  "view_revision": hashlib.sha256(encoded.encode()).hexdigest(),
                  "observed_at": observed_at, "expires_at": expires, "cursor": next_cursor}
        if base is None:
            result.update(kind="snapshot", snapshot=body)
        else:
            result.update(kind="delta", base_cursor=cursor, delta=diff_view(base, body))
        json_text(result)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._admit_actor_tx(c, universe, role_id, identity_token)
            self._check_world_tx(c, universe)
            c.execute("DELETE FROM view_checkpoints WHERE expires_at<=?", (time.time(),))
            c.execute("""INSERT INTO view_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (self._view_cursor_hash(next_cursor), universe, role_id, credential, view,
                       selector_json, definition.version, spec.version, encoded, revision, observed_at, expires))
            # Keep the newly returned checkpoint, evict only older derived cache entries.
            for clause, params, maximum in (
                ("universe=? AND role_id=?", (universe, role_id), VIEW_CACHE_PER_VIEWER),
                ("1=1", (), VIEW_CACHE_MAX),
            ):
                c.execute("DELETE FROM view_checkpoints WHERE cursor_hash IN (SELECT cursor_hash FROM view_checkpoints WHERE "
                          + clause + " ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (*params, maximum))
        return result
