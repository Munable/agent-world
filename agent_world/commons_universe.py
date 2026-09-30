from __future__ import annotations

from .runtime_core import (
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    RoleInactive,
    WorldRuntime,
    WorldRuntimeError,
)
from .runtime_errors import PermissionDenied


POST_SCOPE = "commons:posts"
REPLY_SCOPE_PREFIX = "commons:replies:"
CONVERSATION_SCOPE = "commons:conversations"
CONVERSATION_INDEX_SCOPE_PREFIX = "commons:conversation-index:"
MESSAGE_SCOPE_PREFIX = "commons:messages:"

ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "pattern": "^[A-Za-z0-9_-]+$",
}
ROLE_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 128}
TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 2000}
LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 100}


def _rows(page: dict) -> list[dict]:
    return [
        {
            **row["value"],
            "state_version": row["version"],
            "updated_at": row["updated_at"],
        }
        for row in page["items"]
    ]


def _active_role(ctx: FunctionContext, role_id: str) -> dict:
    role = ctx.get_role(role_id)
    if role["status"] != "active":
        raise RoleInactive(role_id)
    return role


def _conversation(ctx: FunctionContext, conversation_id: str) -> dict:
    conversation = ctx.get_state(
        CONVERSATION_SCOPE,
        "conversation:" + conversation_id,
        None,
    )
    if conversation is None:
        raise WorldRuntimeError("conversation not found")
    return conversation


def _require_conversation_participant(
    ctx: FunctionContext,
    conversation: dict,
) -> None:
    if ctx.actor_role_id not in conversation["participant_role_ids"]:
        raise PermissionDenied("conversation is private to its participants")


def install_commons_universe(
    runtime: WorldRuntime,
    universe: str = "commons",
) -> None:
    def create_post(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        post_id = str(arguments["post_id"])
        key = "post:" + post_id
        if ctx.get_state(POST_SCOPE, key, None) is not None:
            raise WorldRuntimeError("post_id already exists")
        post = {
            "post_id": post_id,
            "author_role_id": ctx.actor_role_id,
            "text": str(arguments["text"]),
            "created_at": ctx.now,
        }
        version = ctx.set_state(POST_SCOPE, key, post)
        return FunctionOutcome({**post, "state_version": version})

    runtime.register_function(
        universe,
        "commons.post.create",
        1,
        "Create one durable public post.",
        {
            "type": "object",
            "properties": {"post_id": ID_SCHEMA, "text": TEXT_SCHEMA},
            "required": ["post_id", "text"],
            "additionalProperties": False,
        },
        create_post,
        access="write",
    )

    def list_posts(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        page = ctx.list_state(
            POST_SCOPE,
            prefix="post:",
            limit=int(arguments.get("limit", 20)),
            order="updated_desc",
        )
        return FunctionOutcome({"posts": _rows(page)})

    runtime.register_function(
        universe,
        "commons.post.list",
        1,
        "Read recent public posts.",
        {
            "type": "object",
            "properties": {"limit": LIMIT_SCHEMA},
            "additionalProperties": False,
        },
        list_posts,
        access="read",
    )

    def create_reply(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        post_id = str(arguments["post_id"])
        post = ctx.get_state(POST_SCOPE, "post:" + post_id, None)
        if post is None:
            raise WorldRuntimeError("post not found")
        reply_id = str(arguments["reply_id"])
        scope = REPLY_SCOPE_PREFIX + post_id
        key = "reply:" + reply_id
        if ctx.get_state(scope, key, None) is not None:
            raise WorldRuntimeError("reply_id already exists for this post")
        reply = {
            "reply_id": reply_id,
            "post_id": post_id,
            "author_role_id": ctx.actor_role_id,
            "text": str(arguments["text"]),
            "created_at": ctx.now,
        }
        version = ctx.set_state(scope, key, reply)
        events = ()
        if post["author_role_id"] != ctx.actor_role_id:
            events = (
                EventSpec(
                    post["author_role_id"],
                    "commons.reply.created",
                    {
                        "post_id": post_id,
                        "reply_id": reply_id,
                        "from_role_id": ctx.actor_role_id,
                    },
                ),
            )
        return FunctionOutcome(
            {**reply, "state_version": version},
            events,
        )

    runtime.register_function(
        universe,
        "commons.reply.create",
        1,
        "Create one durable public reply to an existing post.",
        {
            "type": "object",
            "properties": {
                "post_id": ID_SCHEMA,
                "reply_id": ID_SCHEMA,
                "text": TEXT_SCHEMA,
            },
            "required": ["post_id", "reply_id", "text"],
            "additionalProperties": False,
        },
        create_reply,
        access="write",
    )

    def list_replies(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        post_id = str(arguments["post_id"])
        if ctx.get_state(POST_SCOPE, "post:" + post_id, None) is None:
            raise WorldRuntimeError("post not found")
        page = ctx.list_state(
            REPLY_SCOPE_PREFIX + post_id,
            prefix="reply:",
            limit=int(arguments.get("limit", 50)),
            order="updated_desc",
        )
        return FunctionOutcome({"post_id": post_id, "replies": _rows(page)})

    runtime.register_function(
        universe,
        "commons.reply.list",
        1,
        "Read recent public replies for one post.",
        {
            "type": "object",
            "properties": {"post_id": ID_SCHEMA, "limit": LIMIT_SCHEMA},
            "required": ["post_id"],
            "additionalProperties": False,
        },
        list_replies,
        access="read",
    )

    def open_conversation(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        peer_role_id = str(arguments["peer_role_id"])
        if peer_role_id == ctx.actor_role_id:
            raise WorldRuntimeError("conversation peer must be another role")
        _active_role(ctx, peer_role_id)
        conversation_id = str(arguments["conversation_id"])
        key = "conversation:" + conversation_id
        if ctx.get_state(CONVERSATION_SCOPE, key, None) is not None:
            raise WorldRuntimeError("conversation_id already exists")
        participants = sorted([ctx.actor_role_id, peer_role_id])
        conversation = {
            "conversation_id": conversation_id,
            "participant_role_ids": participants,
            "opened_by_role_id": ctx.actor_role_id,
            "created_at": ctx.now,
        }
        version = ctx.set_state(CONVERSATION_SCOPE, key, conversation)
        for role_id in participants:
            ctx.set_state(
                CONVERSATION_INDEX_SCOPE_PREFIX + role_id,
                key,
                conversation,
            )
        return FunctionOutcome(
            {**conversation, "state_version": version},
            (
                EventSpec(
                    peer_role_id,
                    "commons.conversation.opened",
                    {
                        "conversation_id": conversation_id,
                        "from_role_id": ctx.actor_role_id,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "commons.conversation.open",
        1,
        "Open one private two-participant conversation.",
        {
            "type": "object",
            "properties": {
                "conversation_id": ID_SCHEMA,
                "peer_role_id": ROLE_ID_SCHEMA,
            },
            "required": ["conversation_id", "peer_role_id"],
            "additionalProperties": False,
        },
        open_conversation,
        access="write",
    )

    def list_conversations(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        page = ctx.list_state(
            CONVERSATION_INDEX_SCOPE_PREFIX + ctx.actor_role_id,
            prefix="conversation:",
            limit=int(arguments.get("limit", 50)),
            order="updated_desc",
        )
        return FunctionOutcome({"conversations": _rows(page)})

    runtime.register_function(
        universe,
        "commons.conversation.list",
        1,
        "List private conversations visible to the current participant.",
        {
            "type": "object",
            "properties": {"limit": LIMIT_SCHEMA},
            "additionalProperties": False,
        },
        list_conversations,
        access="read",
    )

    def send_message(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        conversation_id = str(arguments["conversation_id"])
        conversation = _conversation(ctx, conversation_id)
        _require_conversation_participant(ctx, conversation)
        message_id = str(arguments["message_id"])
        scope = MESSAGE_SCOPE_PREFIX + conversation_id
        key = "message:" + message_id
        if ctx.get_state(scope, key, None) is not None:
            raise WorldRuntimeError("message_id already exists for this conversation")
        message = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "sender_role_id": ctx.actor_role_id,
            "text": str(arguments["text"]),
            "created_at": ctx.now,
        }
        version = ctx.set_state(scope, key, message)
        recipients = [
            role_id
            for role_id in conversation["participant_role_ids"]
            if role_id != ctx.actor_role_id
        ]
        events = tuple(
            EventSpec(
                role_id,
                "commons.message.created",
                {
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "from_role_id": ctx.actor_role_id,
                },
            )
            for role_id in recipients
        )
        return FunctionOutcome(
            {**message, "state_version": version},
            events,
        )

    runtime.register_function(
        universe,
        "commons.message.send",
        1,
        "Persist one private message inside an existing conversation.",
        {
            "type": "object",
            "properties": {
                "conversation_id": ID_SCHEMA,
                "message_id": ID_SCHEMA,
                "text": TEXT_SCHEMA,
            },
            "required": ["conversation_id", "message_id", "text"],
            "additionalProperties": False,
        },
        send_message,
        access="write",
    )

    def list_messages(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        conversation_id = str(arguments["conversation_id"])
        conversation = _conversation(ctx, conversation_id)
        _require_conversation_participant(ctx, conversation)
        page = ctx.list_state(
            MESSAGE_SCOPE_PREFIX + conversation_id,
            prefix="message:",
            limit=int(arguments.get("limit", 100)),
            order="updated_desc",
        )
        return FunctionOutcome(
            {
                "conversation_id": conversation_id,
                "messages": _rows(page),
            }
        )

    runtime.register_function(
        universe,
        "commons.message.list",
        1,
        "Read recent messages from a private conversation.",
        {
            "type": "object",
            "properties": {
                "conversation_id": ID_SCHEMA,
                "limit": LIMIT_SCHEMA,
            },
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        list_messages,
        access="read",
    )
