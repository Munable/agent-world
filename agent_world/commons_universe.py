from __future__ import annotations

from .runtime_core import (
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    RoleInactive,
    WorldRuntime,
    WorldRuntimeError,
)
from .runtime_contracts import MAX_RESULT_BYTES, json_text
from .runtime_errors import InvalidArguments, PermissionDenied, ResourceNotFound


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
AFTER_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 4096}


def _rows(page: dict, ctx: FunctionContext) -> list[dict]:
    return [
        {
            **_with_names(row["value"], ctx),
            "state_version": row["version"],
            "updated_at": row["updated_at"],
        }
        for row in page["items"]
    ]


def _page_result(page: dict, field: str, ctx: FunctionContext) -> dict:
    return {
        field: _rows(page, ctx),
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
    }


def _list_result(ctx: FunctionContext, arguments: dict, *, scope: str, prefix: str,
                 field: str, default_limit: int, **context) -> dict:
    limit = int(arguments.get("limit", default_limit))
    while True:
        page = ctx.list_state(scope, prefix=prefix, limit=limit,
                              after=arguments.get("after"), order="updated_desc")
        result = {**context, **_page_result(page, field, ctx)}
        try:
            # Leave room for the Runtime/transport envelope. Re-read a smaller
            # page so the cursor always follows the final returned item.
            json_text(result, maximum=MAX_RESULT_BYTES - 1024)
        except InvalidArguments:
            if limit == 1:
                raise
            limit = max(1, limit // 2)
        else:
            return result


def _with_names(value: dict, ctx: FunctionContext) -> dict:
    result = dict(value)
    for field in ("author_role_id", "sender_role_id"):
        if field in value:
            result[field.replace("role_id", "display_name")] = ctx.get_role(value[field])["display_name"]
    if "participant_role_ids" in value:
        result["participant_names"] = {
            role: ctx.get_role(role)["display_name"] for role in value["participant_role_ids"]
        }
    return result


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

    def get_post(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        post = ctx.get_state(POST_SCOPE, "post:" + arguments["post_id"], None)
        if post is None:
            raise ResourceNotFound("post not found")
        return FunctionOutcome({"post": _with_names(post, ctx)})

    runtime.register_function(
        universe, "commons.post.get", 1, "Read one durable public post by its ID.",
        {"type": "object", "properties": {"post_id": ID_SCHEMA},
         "required": ["post_id"], "additionalProperties": False},
        get_post, access="read",
    )

    def list_posts(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        return FunctionOutcome(_list_result(ctx, arguments, scope=POST_SCOPE, prefix="post:",
                                            field="posts", default_limit=20))

    runtime.register_function(
        universe,
        "commons.post.list",
        2,
        "Read public posts newest first; pass next_cursor as after to read older posts.",
        {
            "type": "object",
            "properties": {"limit": LIMIT_SCHEMA, "after": AFTER_SCHEMA},
            "additionalProperties": False,
        },
        list_posts,
        access="read",
    )

    def create_reply(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        post_id = str(arguments["post_id"])
        post = ctx.get_state(POST_SCOPE, "post:" + post_id, None)
        if post is None:
            raise ResourceNotFound("post not found")
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
            raise ResourceNotFound("post not found")
        return FunctionOutcome(_list_result(ctx, arguments, scope=REPLY_SCOPE_PREFIX + post_id,
                                            prefix="reply:", field="replies", default_limit=50,
                                            post_id=post_id))

    runtime.register_function(
        universe,
        "commons.reply.list",
        2,
        "Read public replies newest first; pass next_cursor as after to read older replies.",
        {
            "type": "object",
            "properties": {"post_id": ID_SCHEMA, "limit": LIMIT_SCHEMA, "after": AFTER_SCHEMA},
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
        return FunctionOutcome(_list_result(ctx, arguments,
                                            scope=CONVERSATION_INDEX_SCOPE_PREFIX + ctx.actor_role_id,
                                            prefix="conversation:", field="conversations", default_limit=50))

    runtime.register_function(
        universe,
        "commons.conversation.list",
        2,
        "List private conversations newest first; pass next_cursor as after to read older conversations.",
        {
            "type": "object",
            "properties": {"limit": LIMIT_SCHEMA, "after": AFTER_SCHEMA},
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
        return FunctionOutcome(_list_result(ctx, arguments, scope=MESSAGE_SCOPE_PREFIX + conversation_id,
                                            prefix="message:", field="messages", default_limit=100,
                                            conversation_id=conversation_id))

    runtime.register_function(
        universe,
        "commons.message.list",
        2,
        "Read private messages newest first; pass next_cursor as after to read older messages.",
        {
            "type": "object",
            "properties": {
                "conversation_id": ID_SCHEMA,
                "limit": LIMIT_SCHEMA,
                "after": AFTER_SCHEMA,
            },
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        list_messages,
        access="read",
    )
