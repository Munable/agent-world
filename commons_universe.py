from __future__ import annotations

import json
import time

from runtime_core import (
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    RoleInactive,
    RoleNotFound,
    WorldRuntime,
    WorldRuntimeError,
)


BOARD_SCOPE = "commons:board"


def install_commons_universe(
    runtime: WorldRuntime,
    universe: str = "commons",
) -> None:
    def create_post(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        post_id = str(arguments["post_id"])
        text = str(arguments["text"])
        key = f"post:{post_id}"
        if ctx.get_state(BOARD_SCOPE, key, None) is not None:
            raise WorldRuntimeError("post_id already exists")
        created_at = time.time()
        post = {
            "post_id": post_id,
            "author_role_id": ctx.actor_role_id,
            "text": text,
            "created_at": created_at,
        }
        version = ctx.set_state(BOARD_SCOPE, key, post)
        return FunctionOutcome(
            result={**post, "state_version": version},
            events=(
                EventSpec(
                    recipient_role_id=ctx.actor_role_id,
                    kind="commons_post_created",
                    payload={
                        "post_id": post_id,
                        "author_role_id": ctx.actor_role_id,
                        "text": text,
                        "state_version": version,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "commons.board.post",
        1,
        "Create one durable public post in the Commons board.",
        {
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[A-Za-z0-9_-]+$",
                },
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 280,
                },
            },
            "required": ["post_id", "text"],
            "additionalProperties": False,
        },
        create_post,
        access="write",
    )
    def list_posts(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        limit = int(arguments.get("limit", 20))
        rows = ctx.conn.execute(
            """SELECT value_json,version,updated_at
               FROM world_state
               WHERE universe=? AND scope=? AND state_key LIKE 'post:%'
               ORDER BY updated_at DESC, state_key DESC
               LIMIT ?""",
            (ctx.universe, BOARD_SCOPE, limit),
        ).fetchall()
        posts = []
        for row in rows:
            post = json.loads(row["value_json"])
            post["state_version"] = int(row["version"])
            post["updated_at"] = float(row["updated_at"])
            posts.append(post)
        return FunctionOutcome(result={"posts": posts})

    runtime.register_function(
        universe,
        "commons.board.list",
        1,
        "Read recent public Commons posts without modifying world state.",
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                }
            },
            "additionalProperties": False,
        },
        list_posts,
        access="read",
    )
    def send_note(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        recipient = str(arguments["recipient_role_id"])
        text = str(arguments["text"])
        role = ctx.conn.execute(
            "SELECT status FROM roles WHERE role_id=?",
            (recipient,),
        ).fetchone()
        if role is None:
            raise RoleNotFound(recipient)
        if role["status"] != "active":
            raise RoleInactive(recipient)
        return FunctionOutcome(
            result={
                "delivered_to": recipient,
                "text": text,
            },
            events=(
                EventSpec(
                    recipient_role_id=recipient,
                    kind="commons_note",
                    payload={
                        "from_role_id": ctx.actor_role_id,
                        "text": text,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "commons.note.send",
        1,
        "Send one durable note event to another active role.",
        {
            "type": "object",
            "properties": {
                "recipient_role_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 280,
                },
            },
            "required": ["recipient_role_id", "text"],
            "additionalProperties": False,
        },
        send_note,
        access="write",
    )
