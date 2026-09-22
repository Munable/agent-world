from __future__ import annotations

import json
import time

from runtime_core import (
    EventSpec,
    FunctionContext,
    FunctionOutcome,
    WorldRuntime,
    WorldRuntimeError,
)


START_PLACE = "threshold"

PLACES = {
    "threshold": {
        "place_id": "threshold",
        "name": "The Threshold",
        "summary": "A quiet arrival point where new roles first enter the world.",
    },
    "garden": {
        "place_id": "garden",
        "name": "The Quiet Garden",
        "summary": "A shared place for small traces, observations, and returns.",
    },
    "observatory": {
        "place_id": "observatory",
        "name": "The Observatory",
        "summary": "A high, sparse room for looking outward and leaving signals.",
    },
}


def _current_place(ctx: FunctionContext) -> str:
    scope = f"role:{ctx.actor_role_id}"
    place_id = str(ctx.get_state(scope, "world_zero.location", START_PLACE))
    return place_id if place_id in PLACES else START_PLACE
def _place_marks(
    ctx: FunctionContext,
    place_id: str,
    limit: int,
) -> list[dict]:
    rows = ctx.conn.execute(
        """SELECT value_json,version,updated_at
           FROM world_state
           WHERE universe=? AND scope=? AND state_key LIKE 'mark:%'
           ORDER BY updated_at DESC, state_key DESC
           LIMIT ?""",
        (ctx.universe, f"place:{place_id}", limit),
    ).fetchall()
    marks = []
    for row in rows:
        mark = json.loads(row["value_json"])
        mark["state_version"] = int(row["version"])
        mark["updated_at"] = float(row["updated_at"])
        marks.append(mark)
    return marks


def install_world_zero(
    runtime: WorldRuntime,
    universe: str = "world-zero",
) -> None:
    def list_places(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        current = _current_place(ctx)
        return FunctionOutcome(
            result={
                "current_place_id": current,
                "places": list(PLACES.values()),
            }
        )

    runtime.register_function(
        universe,
        "world.list_places",
        1,
        "List the small set of places currently available in World Zero.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        list_places,
        access="read",
    )
    def observe(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        current = _current_place(ctx)
        limit = int(arguments.get("mark_limit", 8))
        return FunctionOutcome(
            result={
                "place": PLACES[current],
                "marks": _place_marks(ctx, current, limit),
            }
        )

    runtime.register_function(
        universe,
        "world.observe",
        1,
        "Observe the current place and recent durable marks left there.",
        {
            "type": "object",
            "properties": {
                "mark_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                }
            },
            "additionalProperties": False,
        },
        observe,
        access="read",
    )

    def visit(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        destination = str(arguments["place_id"])
        if destination not in PLACES:
            raise WorldRuntimeError(f"unknown place: {destination}")
        previous = _current_place(ctx)
        version = ctx.set_state(
            f"role:{ctx.actor_role_id}",
            "world_zero.location",
            destination,
        )
        return FunctionOutcome(
            result={
                "from_place_id": previous,
                "place": PLACES[destination],
                "state_version": version,
            },
            events=(
                EventSpec(
                    recipient_role_id=ctx.actor_role_id,
                    kind="world_zero_visited",
                    payload={
                        "from_place_id": previous,
                        "place_id": destination,
                        "state_version": version,
                    },
                ),
            ),
        )
    runtime.register_function(
        universe,
        "world.visit",
        1,
        "Move the current role to another place in World Zero.",
        {
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "enum": list(PLACES),
                }
            },
            "required": ["place_id"],
            "additionalProperties": False,
        },
        visit,
        access="write",
    )

    def leave_mark(
        ctx: FunctionContext,
        arguments: dict,
    ) -> FunctionOutcome:
        place_id = _current_place(ctx)
        mark_id = str(arguments["mark_id"])
        key = f"mark:{mark_id}"
        scope = f"place:{place_id}"
        if ctx.get_state(scope, key, None) is not None:
            raise WorldRuntimeError("mark_id already exists at this place")

        role = ctx.conn.execute(
            """SELECT display_name
               FROM roles
               WHERE role_id=?""",
            (ctx.actor_role_id,),
        ).fetchone()
        author_name = (
            role["display_name"] if role is not None else ctx.actor_role_id
        )
        mark = {
            "mark_id": mark_id,
            "place_id": place_id,
            "author_role_id": ctx.actor_role_id,
            "author_display_name": author_name,
            "text": str(arguments["text"]),
            "created_at": time.time(),
        }
        version = ctx.set_state(scope, key, mark)
        return FunctionOutcome(
            result={**mark, "state_version": version},
            events=(
                EventSpec(
                    recipient_role_id=ctx.actor_role_id,
                    kind="world_zero_mark_left",
                    payload={
                        "place_id": place_id,
                        "mark_id": mark_id,
                        "state_version": version,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "world.leave_mark",
        1,
        "Leave one durable public mark at the role's current place.",
        {
            "type": "object",
            "properties": {
                "mark_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[A-Za-z0-9_-]+$",
                },
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
            },
            "required": ["mark_id", "text"],
            "additionalProperties": False,
        },
        leave_mark,
        access="write",
    )
