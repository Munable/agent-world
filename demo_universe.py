from __future__ import annotations

from runtime_core import EventSpec, FunctionContext, FunctionOutcome, WorldRuntime


def install_demo_universe(runtime: WorldRuntime, universe: str = "demo") -> None:
    def increment_counter(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        amount = int(arguments["amount"])
        scope = f"role:{ctx.actor_role_id}"
        current = int(ctx.get_state(scope, "counter", 0))
        new_value = current + amount
        version = ctx.set_state(scope, "counter", new_value)
        return FunctionOutcome(
            result={"value": new_value, "state_version": version},
            events=(
                EventSpec(
                    recipient_role_id=ctx.actor_role_id,
                    kind="state_changed",
                    payload={
                        "scope": scope,
                        "key": "counter",
                        "value": new_value,
                        "version": version,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "counter.increment",
        1,
        "Increment the current role's demo counter.",
        {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "minimum": -10, "maximum": 10}
            },
            "required": ["amount"],
            "additionalProperties": False,
        },
        increment_counter,
        access="write",
    )

    def get_counter(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        scope = f"role:{ctx.actor_role_id}"
        current = int(ctx.get_state(scope, "counter", 0))
        return FunctionOutcome(result={"value": current})

    runtime.register_function(
        universe,
        "counter.get",
        1,
        "Read the current role's demo counter without modifying world state.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        get_counter,
        access="read",
    )

    def add_activity_score(ctx: FunctionContext, arguments: dict) -> FunctionOutcome:
        if ctx.activity_id is None:
            raise RuntimeError("activity claim was not attached")
        delta = int(arguments["delta"])
        scope = f"activity:{ctx.activity_id}"
        current = int(ctx.get_state(scope, "score", 0))
        new_value = current + delta
        version = ctx.set_state(scope, "score", new_value)
        return FunctionOutcome(
            result={
                "activity_id": ctx.activity_id,
                "score": new_value,
                "state_version": version,
            },
            events=(
                EventSpec(
                    recipient_role_id=ctx.actor_role_id,
                    kind="activity_state_changed",
                    payload={
                        "activity_id": ctx.activity_id,
                        "key": "score",
                        "value": new_value,
                        "version": version,
                    },
                ),
            ),
        )

    runtime.register_function(
        universe,
        "activity.score.add",
        1,
        "Add score to an activity. A live activity claim is required.",
        {
            "type": "object",
            "properties": {
                "delta": {"type": "integer", "minimum": 1, "maximum": 100}
            },
            "required": ["delta"],
            "additionalProperties": False,
        },
        add_activity_score,
        access="write",
        requires_activity_claim=True,
    )
