from .world_sdk import StateRule, definition_from_installer


def get_builtin_definition(profile):
    if profile == "demo":
        from .demo_universe import install_demo_universe

        def view(ctx):
            return {"counter": ctx.get_state(f"role:{ctx.actor_role_id}", "counter", 0)}

        rules = (
            StateRule("role:", "counter", {"type": "integer"}),
            StateRule("activity:", "score", {"type": "integer"}),
        )
        return definition_from_installer(
            install_demo_universe, "demo", "Demo", state_rules=rules, bootstrap=view
        )
    if profile == "commons":
        from .commons_universe import install_commons_universe

        def view(ctx):
            return {
                "recent_posts": [
                    item["value"]
                    for item in ctx.list_state(
                        "commons:board", prefix="post:", limit=5, order="updated_desc"
                    )["items"]
                ]
            }

        return definition_from_installer(
            install_commons_universe,
            "commons",
            "Commons",
            state_rules=(StateRule("commons:board", "post:", {"type": "object"}),),
            bootstrap=view,
        )
    if profile in {"world-zero", "world_zero", "zero"}:
        from .world_zero_universe import install_world_zero, _current_place, _place_marks, PLACES

        def view(ctx):
            place = _current_place(ctx)
            return {"place": PLACES[place], "marks": _place_marks(ctx, place, 5)}

        return definition_from_installer(
            install_world_zero,
            "world-zero",
            "World Zero",
            state_rules=(
                StateRule("role:", "world_zero.location", {"type": "string", "enum": list(PLACES)}),
                StateRule("place:", "mark:", {"type": "object"}),
            ),
            bootstrap=view,
            entry_instructions="Call world.list_places and world.observe. Choose a place, call world.visit, "
            "then world.leave_mark once with a short non-secret message. Observe again and report what changed. "
            "Use a stable operation_id for each write and reuse it only when retrying that same intent.",
        )
    raise ValueError(f"unknown built-in world: {profile}")
