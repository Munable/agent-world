"""A bounded cooperative adventure; all domain rules stay in this SDK consumer.

Trusted server setup may enter key identities before installing make_world with
their world-local role IDs. Those arguments ONLY initialize a new instance.
Reinstallation, including the default WORLD, preserves its stored authority.
Changing factory arguments is not a grant/revocation: the stored founder must
call camp.set_judge. WORLD has no founder or judges and never grants first-entry
privileges. No model, private reasoning, or resident Agent loop runs here.
"""
from __future__ import annotations

from agent_world import EventSpec, FunctionOutcome, FunctionSpec, StateRule, TimerSpec, WorldDefinition
from agent_world.errors import RuleViolation, StateConflict


ID = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_-]+$"}
ROLE = {"type": "string", "minLength": 1, "maxLength": 128}
TEXT = {"type": "string", "minLength": 1, "maxLength": 3000}
VERSION = {"type": "integer", "minimum": 1}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def object_schema(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


COST = object_schema({name: {"type": "integer", "minimum": 0, "maximum": 20}
                      for name in ("wood", "rope", "stone")})
EFFECTS = object_schema({"passage": {"enum": ["foot", "cargo"]},
                         "stability": {"type": "integer", "minimum": 1, "maximum": 3},
                         "exposure": {"type": "integer", "minimum": 0, "maximum": 2}})
ROUTE = object_schema({"passage": {"enum": ["blocked", "foot", "cargo"]},
                       "stability": {"type": "integer", "minimum": 0, "maximum": 3},
                       "exposure": {"type": "integer", "minimum": 0, "maximum": 2}})
AUTHORITY = object_schema({"founder_role_id": {"anyOf": [ROLE, {"type": "null"}]},
                           "judges": {"type": "array", "items": ROLE, "maxItems": 16, "uniqueItems": True},
                           "epoch": VERSION})
SITES = {
    "bank": "The narrow bank permits foot traffic; wider cargo needs more timber. Exposure ranges from 0 sheltered to 2 exposed.",
    "bridge": "Stability ranges from 1 to 3. Each extra level uses a stone anchor; sheltered crossings require more rope.",
    "store": "The shared finite materials are wood, rope and stone. They are consumed only when an owner accepts an approved plan.",
}
FINDINGS = object_schema({site: {"type": "boolean"} for site in SITES})
MEMBER = object_schema({"joined_at": {"type": "number"}})
NOTE = object_schema({"owner": ROLE, "content": TEXT})
RULING = object_schema({"judge": ROLE, "reason": TEXT, "decision": {"enum": ["approve", "decline"]},
                        "authority_epoch": VERSION, "inventory_version": VERSION, "route_version": VERSION,
                        "cost": {"anyOf": [COST, {"type": "null"}]},
                        "effects": {"anyOf": [EFFECTS, {"type": "null"}]}, "decided_at": {"type": "number"}})
INTENT = object_schema({"owner": ROLE, "text": TEXT, "status": {"enum": ["pending", "approved", "declined", "executed"]},
                        "created_at": {"type": "number"}, "ruling": {"anyOf": [RULING, {"type": "null"}]}})
PROJECT = object_schema({"owner": ROLE, "title": TEXT, "status": {"enum": ["active", "completed", "expired"]},
                         "due_at": {"type": "number"}, "timer_id": {"type": "string"},
                         "settled_at": {"type": ["number", "null"]}})
PROPOSE_ARGS = object_schema({"intent_id": ID, "text": TEXT, "private_note": TEXT}, ["intent_id", "text"])
JUDGE_ARGS = object_schema({"intent_id": ID, "expected_intent_version": VERSION,
                            "inventory_version": VERSION, "route_version": VERSION, "authority_epoch": VERSION,
                            "decision": {"enum": ["approve", "decline"]}, "reason": TEXT,
                            "cost": COST, "effects": EFFECTS},
                           ["intent_id", "expected_intent_version", "inventory_version", "route_version",
                            "authority_epoch", "decision", "reason"])
ACCEPT_ARGS = object_schema({"intent_id": ID, "expected_intent_version": VERSION})
SET_JUDGE_ARGS = object_schema({"role_id": ROLE, "enabled": {"type": "boolean"}, "authority_epoch": VERSION})
EXPLORE_ARGS = object_schema({"site": {"enum": list(SITES)}})
START_PROJECT_ARGS = object_schema({"project_id": ID, "title": TEXT,
                                    "seconds": {"type": "number", "minimum": 0.1, "maximum": 86400}})
COMPLETE_PROJECT_ARGS = object_schema({"project_id": ID, "expected_project_version": VERSION})
PROJECT_ID_ARGS = object_schema({"project_id": ID})


def required_cost(effects):
    """Finite physical effects, with composable tradeoffs rather than named plans."""
    return {"wood": 2 if effects["passage"] == "foot" else 4,
            "rope": effects["stability"] + 2 - effects["exposure"],
            "stone": effects["stability"] - 1}


def goal_reached(route):
    return route["passage"] == "cargo" and route["stability"] >= 2 and route["exposure"] <= 1


def _authority(ctx):
    return ctx.get_state("camp:world", "authority")


def _member(ctx, args=None):
    return ctx.get_state("camp:members", ctx.actor_role_id) is not None


def _judge(ctx, args=None):
    return _member(ctx) and ctx.actor_role_id in _authority(ctx)["judges"]


def _founder(ctx, args=None):
    return ctx.actor_role_id == _authority(ctx)["founder_role_id"]


def _check_version(record, expected, label):
    if record["version"] != expected:
        raise StateConflict(label + " changed; inspect again and request a fresh ruling")


def _intent(ctx, intent_id):
    record = ctx.get_state_record("camp:intents", intent_id)
    if not record["exists"]:
        raise RuleViolation("intent does not exist")
    return record


def _events(ctx, kind, payload):
    return tuple(EventSpec(item["key"], kind, payload)
                 for item in ctx.list_state("camp:members", limit=200)["items"])


def _state_access(ctx, scope, key, access):
    if access == "read":
        if scope == "camp:world" and key == "authority":
            return True
        if scope == "camp:notes":
            note = ctx.authorization_state(scope, key)
            authority = ctx.authorization_state("camp:world", "authority")
            return (note is None or note["owner"] == ctx.actor_role_id
                    or ctx.actor_role_id in authority["judges"]
                    and ctx.authorization_state("camp:members", ctx.actor_role_id) is not None)
        if scope == "camp:members" and key == ctx.actor_role_id:
            return True
        if scope == "camp:members" and ctx.function_id in {"camp.join", "camp.set_judge"}:
            return True
        return (ctx.actor_role_id == "system:timer" and ctx.function_id == "timer:camp.expire_project"
                or ctx.authorization_state("camp:members", ctx.actor_role_id) is not None)
    # This constrains even ordinary SDK writes; no generic patch tool is exposed.
    if ctx.function_id == "camp.join":
        return scope == "camp:members" and key == ctx.actor_role_id
    if ctx.function_id == "camp.set_judge":
        authority = ctx.authorization_state("camp:world", "authority")
        return scope == "camp:world" and key == "authority" and ctx.actor_role_id == authority["founder_role_id"]
    if ctx.function_id == "timer:camp.expire_project":
        return scope == "camp:projects" and ctx.actor_role_id == "system:timer"
    if ctx.authorization_state("camp:members", ctx.actor_role_id) is None:
        return False
    if ctx.function_id == "camp.explore":
        return scope == "camp:world" and key == "findings"
    if ctx.function_id == "camp.propose":
        value = ctx.authorization_state(scope, key)
        return scope in {"camp:intents", "camp:notes"} and (value is None or value["owner"] == ctx.actor_role_id)
    if ctx.function_id == "camp.judge":
        return scope == "camp:intents" and ctx.actor_role_id in ctx.authorization_state("camp:world", "authority")["judges"]
    if ctx.function_id == "camp.accept":
        if scope == "camp:world" and key in {"inventory", "route"}:
            return True
        value = ctx.authorization_state(scope, key)
        return scope == "camp:intents" and value is not None and value["owner"] == ctx.actor_role_id
    if ctx.function_id in {"camp.start_project", "camp.complete_project"}:
        value = ctx.authorization_state(scope, key)
        return scope == "camp:projects" and (value is None or value["owner"] == ctx.actor_role_id)
    return False


def join(ctx, args):
    record = ctx.get_state_record("camp:members", ctx.actor_role_id)
    if not record["exists"]:
        # The member cap also keeps one domain broadcast within the SDK event cap.
        # Read other members through this declared function, before admission.
        if len(ctx.list_state("camp:members", limit=200)["items"]) >= 16:
            raise RuleViolation("camp is full")
        ctx.set_state("camp:members", ctx.actor_role_id, {"joined_at": ctx.now}, expected_version=0)
    return FunctionOutcome({"role_id": ctx.actor_role_id, "joined": True})


def inspect(ctx, args):
    records = {key: ctx.get_state_record("camp:world", key) for key in ("inventory", "route", "authority", "findings")}
    intents = ctx.list_state("camp:intents", limit=200)["items"]
    notes = []
    for item in intents:
        if item["value"]["owner"] == ctx.actor_role_id or _judge(ctx):
            note = ctx.get_state_record("camp:notes", item["key"])
            if note["exists"]:
                notes.append({"key": item["key"], **note})
    return FunctionOutcome({"role_id": ctx.actor_role_id, **records,
                            "members": ctx.list_state("camp:members", limit=200)["items"],
                            "intents": intents, "private_notes": notes,
                            "projects": ctx.list_state("camp:projects", limit=200)["items"],
                            "goal_reached": goal_reached(records["route"]["value"]), "now": ctx.now,
                            "goal": "Restore cargo passage with stability >= 2 and exposure <= 1.",
                            "rules": {"effect_limits": EFFECTS, "cost_formula":
                                      "wood=2 for foot or 4 for cargo; rope=stability+2-exposure; stone=stability-1",
                                      "sites": list(SITES), "public_intents": True}})


def explore(ctx, args):
    record = ctx.get_state_record("camp:world", "findings")
    findings = record["value"]
    if not findings[args["site"]]:
        findings[args["site"]] = True
        ctx.set_state("camp:world", "findings", findings, expected_version=record["version"])
    result = {"site": args["site"], "clue": SITES[args["site"]]}
    return FunctionOutcome(result, _events(ctx, "camp.explored", result))


def propose(ctx, args):
    if ctx.get_state("camp:intents", args["intent_id"]) is not None:
        raise RuleViolation("intent_id already exists; inspect or reuse the original operation_id")
    if not args["text"].strip():
        raise RuleViolation("describe an intended action")
    if len(ctx.list_state("camp:intents", limit=200)["items"]) >= 100:
        raise RuleViolation("this bounded trial allows at most 100 intents")
    value = {"owner": ctx.actor_role_id, "text": args["text"], "status": "pending", "created_at": ctx.now, "ruling": None}
    version = ctx.set_state("camp:intents", args["intent_id"], value, expected_version=0)
    if "private_note" in args:
        if not args["private_note"].strip():
            raise RuleViolation("private_note must contain material")
        ctx.set_state("camp:notes", args["intent_id"], {"owner": ctx.actor_role_id, "content": args["private_note"]}, expected_version=0)
    result = {"intent_id": args["intent_id"], "intent": value, "intent_version": version}
    return FunctionOutcome(result, _events(ctx, "camp.proposed", result))


def set_judge(ctx, args):
    record = ctx.get_state_record("camp:world", "authority")
    authority = record["value"]
    if authority["epoch"] != args["authority_epoch"]:
        raise StateConflict("authority changed; inspect its current epoch")
    if ctx.get_role(args["role_id"])["status"] != "active":
        raise RuleViolation("judge must be an active world-local role")
    if args["enabled"] == (args["role_id"] in authority["judges"]):
        return FunctionOutcome({"authority": authority, "authority_version": record["version"]})
    if args["enabled"]:
        if args["role_id"] not in authority["judges"]:
            if len(authority["judges"]) >= 16:
                raise RuleViolation("too many judges")
            authority["judges"].append(args["role_id"])
    elif args["role_id"] in authority["judges"]:
        authority["judges"].remove(args["role_id"])
    else:
        return FunctionOutcome({"authority": authority, "authority_version": record["version"]})
    authority["epoch"] += 1
    version = ctx.set_state("camp:world", "authority", authority, expected_version=record["version"])
    result = {"authority": authority, "authority_version": version}
    return FunctionOutcome(result, _events(ctx, "camp.authority_changed", result))


def judge(ctx, args):
    record = _intent(ctx, args["intent_id"])
    _check_version(record, args["expected_intent_version"], "intent")
    if record["value"]["status"] == "executed":
        raise RuleViolation("an executed intent cannot be reinterpreted")
    authority = _authority(ctx)
    if authority["epoch"] != args["authority_epoch"]:
        raise StateConflict("judge authority changed; inspect again")
    for key in ("inventory", "route"):
        _check_version(ctx.get_state_record("camp:world", key), args[key + "_version"], key)
    if not args["reason"].strip():
        raise RuleViolation("a ruling requires a public reason")
    cost, effects = args.get("cost"), args.get("effects")
    if args["decision"] == "approve":
        if cost is None or effects is None:
            raise RuleViolation("approval needs cost and effects")
        if cost != required_cost(effects):
            raise RuleViolation("cost does not follow the declared physical effect formula")
        inventory = ctx.get_state("camp:world", "inventory")
        if any(cost[name] > inventory[name] for name in cost):
            raise RuleViolation("the shared stock cannot fund this plan")
    elif cost is not None or effects is not None:
        raise RuleViolation("a declined intent cannot carry executable cost or effects")
    ruling = {"judge": ctx.actor_role_id, "reason": args["reason"], "decision": args["decision"],
              "authority_epoch": authority["epoch"], "inventory_version": args["inventory_version"],
              "route_version": args["route_version"], "cost": cost, "effects": effects, "decided_at": ctx.now}
    value = record["value"]
    value.update(status="approved" if args["decision"] == "approve" else "declined", ruling=ruling)
    version = ctx.set_state("camp:intents", args["intent_id"], value, expected_version=record["version"])
    result = {"intent_id": args["intent_id"], "intent": value, "intent_version": version}
    return FunctionOutcome(result, _events(ctx, "camp.judged", result))


def accept(ctx, args):
    record = _intent(ctx, args["intent_id"])
    if record["value"]["owner"] != ctx.actor_role_id:
        raise RuleViolation("only the proposer can accept their own plan")
    _check_version(record, args["expected_intent_version"], "intent")
    value = record["value"]
    if value["status"] != "approved":
        raise RuleViolation("intent has no executable approved ruling")
    ruling = value["ruling"]
    authority = _authority(ctx)
    if authority["epoch"] != ruling["authority_epoch"] or ruling["judge"] not in authority["judges"]:
        raise StateConflict("judge authority changed; obtain a fresh ruling")
    if ctx.get_role(ruling["judge"])["status"] != "active":
        raise RuleViolation("the approving judge is inactive")
    inventory = ctx.get_state_record("camp:world", "inventory")
    route = ctx.get_state_record("camp:world", "route")
    _check_version(inventory, ruling["inventory_version"], "inventory")
    _check_version(route, ruling["route_version"], "route")
    stock = inventory["value"]
    if any(ruling["cost"][name] > stock[name] for name in stock):
        raise RuleViolation("the shared stock can no longer fund this plan")
    stock = {name: stock[name] - ruling["cost"][name] for name in stock}
    inventory_version = ctx.set_state("camp:world", "inventory", stock, expected_version=inventory["version"])
    route_version = ctx.set_state("camp:world", "route", ruling["effects"], expected_version=route["version"])
    value["status"] = "executed"
    intent_version = ctx.set_state("camp:intents", args["intent_id"], value, expected_version=record["version"])
    result = {"intent_id": args["intent_id"], "status": "executed", "inventory": stock,
              "route": ruling["effects"], "inventory_version": inventory_version,
              "route_version": route_version, "intent_version": intent_version,
              "goal_reached": goal_reached(ruling["effects"])}
    return FunctionOutcome(result, _events(ctx, "camp.executed", result))


def start_project(ctx, args):
    if ctx.get_state("camp:projects", args["project_id"]) is not None:
        raise RuleViolation("project_id already exists")
    if not args["title"].strip():
        raise RuleViolation("describe the timed project")
    if len(ctx.list_state("camp:projects", limit=200)["items"]) >= 30:
        raise RuleViolation("this bounded trial allows at most 30 projects")
    due_at = ctx.now + args["seconds"]
    timer_id = "camp-project:" + args["project_id"]
    value = {"owner": ctx.actor_role_id, "title": args["title"], "status": "active", "due_at": due_at,
             "timer_id": timer_id, "settled_at": None}
    version = ctx.set_state("camp:projects", args["project_id"], value, expected_version=0)
    ctx.schedule_timer(timer_id, "camp.expire_project", {"project_id": args["project_id"]}, due_at=due_at)
    result = {"project_id": args["project_id"], "project": value, "project_version": version}
    return FunctionOutcome(result, _events(ctx, "camp.project_started", result))


def complete_project(ctx, args):
    record = ctx.get_state_record("camp:projects", args["project_id"])
    if not record["exists"] or record["value"]["owner"] != ctx.actor_role_id:
        raise RuleViolation("only the project owner can complete it")
    _check_version(record, args["expected_project_version"], "project")
    value = record["value"]
    if value["status"] != "active":
        raise RuleViolation("project already has a terminal outcome")
    if ctx.now >= value["due_at"]:
        raise RuleViolation("deadline reached; wait for the durable expiration")
    value.update(status="completed", settled_at=ctx.now)
    version = ctx.set_state("camp:projects", args["project_id"], value, expected_version=record["version"])
    ctx.cancel_timer(value["timer_id"])
    result = {"project_id": args["project_id"], "project": value, "project_version": version}
    return FunctionOutcome(result, _events(ctx, "camp.project_completed", result))


def expire_project(ctx, args):
    record = ctx.get_state_record("camp:projects", args["project_id"])
    if not record["exists"] or record["value"]["status"] != "active":
        return FunctionOutcome({"project_id": args["project_id"], "changed": False})
    value = record["value"]
    if ctx.now < value["due_at"]:
        raise RuleViolation("project cannot expire before its deadline")
    value.update(status="expired", settled_at=ctx.now)
    version = ctx.set_state("camp:projects", args["project_id"], value, expected_version=record["version"])
    result = {"project_id": args["project_id"], "project": value, "project_version": version, "changed": True}
    return FunctionOutcome(result, _events(ctx, "camp.project_expired", result))


def bootstrap(ctx):
    authority = _authority(ctx)
    return {"role_id": ctx.actor_role_id, "joined": _member(ctx),
            "is_judge": ctx.actor_role_id in authority["judges"],
            "is_founder": ctx.actor_role_id == authority["founder_role_id"],
            "next": "camp.inspect" if _member(ctx) else "camp.join"}


def make_world(*, founder_role_id=None, judge_role_ids=()):
    """Trusted server-only initialization. Arguments never override stored ACLs."""
    configured_judges = tuple(judge_role_ids)
    initial_authority = {"founder_role_id": founder_role_id, "judges": list(configured_judges), "epoch": 1}

    def initialize(ctx):
        for role_id in ([founder_role_id] if founder_role_id is not None else []) + list(configured_judges):
            if ctx.get_role(role_id)["status"] != "active":
                raise RuleViolation("trusted configured authorities must be active world-local roles")
        ctx.set_state("camp:world", "authority", initial_authority, expected_version=0)
        ctx.set_state("camp:world", "inventory", {"wood": 6, "rope": 4, "stone": 3}, expected_version=0)
        ctx.set_state("camp:world", "route", {"passage": "blocked", "stability": 0, "exposure": 2}, expected_version=0)
        ctx.set_state("camp:world", "findings", {site: False for site in SITES}, expected_version=0)

    functions = (
        FunctionSpec("camp.join", join, EMPTY, description="Join the camp as an ordinary member. This grants no judge or founder authority."),
        FunctionSpec("camp.inspect", inspect, EMPTY, access="read", authorize=_member,
                     description="Read shared materials, route, public findings, authority epochs, all pending and historical intents/projects with their state versions."),
        FunctionSpec("camp.explore", explore, EXPLORE_ARGS, authorize=_member,
                     description="Investigate bank, bridge or store. Findings are shared publicly; exploration never creates free materials."),
        FunctionSpec("camp.propose", propose, PROPOSE_ARGS, authorize=_member,
                     description="Persist a free-form intended action publicly for all camp members. Optional private_note is readable only by its owner and currently authorized member judges. Text is pending evidence, not a world effect or another person's consent."),
        FunctionSpec("camp.judge", judge, JUDGE_ARGS, authorize=_judge, visible_to=lambda ctx: _judge(ctx),
                     description="Authorized external judge: explain an intent and approve structured physical effects with exact declared cost, or decline with reason. Read current intent/inventory/route versions and authority epoch first. Reasons are public: do not quote private notes. Approval does not execute or imply proposer consent."),
        FunctionSpec("camp.accept", accept, ACCEPT_ARGS, authorize=_member,
                     description="Only the original proposer accepts an approved plan. Atomically spend shared materials and change passage, rechecking snapshot versions and judge authority. On conflict inspect and obtain a fresh ruling; reuse operation_id for transport retries."),
        FunctionSpec("camp.set_judge", set_judge, SET_JUDGE_ARGS, authorize=_founder, visible_to=lambda ctx: _founder(ctx),
                     description="Only the trusted configured founder grants/revokes a world-local judge. Requires current authority epoch. Membership and text never grant judge power."),
        FunctionSpec("camp.start_project", start_project, START_PROJECT_ARGS, authorize=_member,
                     description="Create an independent owner-completed timed camp commitment (0.1 to 86400 seconds). A durable system timer expires it even while offline; this does not repair the route."),
        FunctionSpec("camp.complete_project", complete_project, COMPLETE_PROJECT_ARGS, authorize=_member,
                     description="Owner declares their timed commitment complete before its real deadline, with current project version. Completion cancels expiration; after deadline it must expire. This declaration does not repair the route."),
    )
    return WorldDefinition("river-camp-example", "River Camp", functions, initialize=initialize, bootstrap=bootstrap,
                           state_authorizer=_state_access,
                           state_rules=(StateRule("camp:world", "authority", AUTHORITY), StateRule("camp:world", "inventory", COST),
                                        StateRule("camp:world", "route", ROUTE), StateRule("camp:world", "findings", FINDINGS),
                                        StateRule("camp:members", "", MEMBER), StateRule("camp:intents", "", INTENT),
                                        StateRule("camp:notes", "", NOTE), StateRule("camp:projects", "", PROJECT)),
                           timers=(TimerSpec("camp.expire_project", expire_project, PROJECT_ID_ARGS),),
                           entry_instructions="Join with camp.join, inspect current state, explore shared clues, then propose an original way to restore passage. All submitted text and ruling reasons are public to camp members. Only a server-configured or founder-authorized external judge may interpret intentions. Accept your own approved plan to spend materials and commit effects. Reach cargo passage with stability >=2 and exposure <=1. Timed projects are separate commitments, not route restoration. Judge cost must be wood=2 foot/4 cargo, rope=stability+2-exposure, stone=stability-1. Inspect versions before judging/accepting. Default WORLD grants no privileges; server factory arguments only initialize a fresh instance, never modify persisted authority on restart.")


WORLD = make_world()
