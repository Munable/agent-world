"""Bounded declarative programs and pure process calculations.

The world owns authorization, definition versions, quantities and persistence.
This module accepts data only: no Python, attribute traversal or world writes.
Mechanical values use logical turns per step, rather than physical SI units.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import math
import re

from .registry import MaterialDef

MAX_EXPRESSION_DEPTH = 8
MAX_EXPRESSION_NODES = 64
MAX_FIELDS = 16
MAX_INTEGER = 1_000_000
_SYMBOL = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
_RESERVED_FIELDS = {
    "id", "entity_id", "owner", "actor", "author", "x", "y", "z",
    "coordinates", "physical_parent", "permissions", "permission", "access",
    "capabilities", "lifecycle", "matter", "components", "version",
}
_EVENT_FIELDS = {
    "kind": "string", "action": "string", "entity_id": "string", "actor": "string",
    "mechanical_position": "integer", "passable": "boolean",
}
_OBJECT_FIELDS = {
    "intact": "boolean", "barrier.active": "boolean", "barrier.opened_sequence": "integer",
    "mechanical.position": "integer", "mechanical.speed": "integer",
    "mechanical.angle": "integer", "mechanical.energy": "integer", "mechanical.status": "string",
}
_CONTRACT_FIELDS = {
    "status": "string", "started_event_sequence": "integer", "deadline_tick": "integer",
    "delivered": "integer", "quantity": "integer",
}
_MECHANICAL_FIELDS = {
    "position": "integer", "speed": "integer", "angle": "integer",
    "status": "string", "available_torque": "integer", "energy": "integer",
}
_BINARY = {"eq", "ne", "lt", "lte", "gt", "gte", "add", "sub", "mul", "div", "min", "max"}
_MECHANICAL_DEFAULTS = {"teeth": 1, "torque": 1, "speed": 1, "energy_per_step": 1, "load": 0, "travel": 100, "pitch": 1}
_MECHANICAL_RANGES = {
    "teeth": (1, 100), "torque": (1, 100), "speed": (1, 10),
    "energy_per_step": (1, 100), "load": (0, 100), "travel": (1, 100), "pitch": (1, 10),
}
_RESERVED_ACTIONS = {
    "charge", "fuel", "connect", "disconnect", "start_recording", "stop_recording",
    "read_records", "offer", "accept", "deliver", "release", "start", "stop",
}


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _mapping(value, path, allowed=None, required=()):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail(path, "must be an object with string keys")
    if allowed is not None and set(value) - set(allowed):
        _fail(path, "unknown keys: " + ", ".join(sorted(set(value) - set(allowed))))
    if set(required) - set(value):
        _fail(path, "missing keys: " + ", ".join(sorted(set(required) - set(value))))
    return value


def _text(value, path, maximum=160, pattern=None):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(path, f"must be a nonempty string of at most {maximum} characters")
    if pattern is not None and not pattern.fullmatch(value):
        _fail(path, "must start with a lowercase letter and use lowercase letters, digits or allowed separators")
    return value


def _integer(value, path, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(path, f"must be an integer from {minimum} to {maximum}")
    return value


def _boolean(value, path):
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _list(value, path, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        _fail(path, f"must be a list of at most {maximum} entries")
    return value


def _fields(definitions, path):
    _mapping(definitions, path)
    if len(definitions) > MAX_FIELDS:
        _fail(path, f"at most {MAX_FIELDS} fields or parameters are allowed")
    result = {}
    for name, spec in definitions.items():
        _text(name, path + "." + name, 48, _SYMBOL)
        if name in _RESERVED_FIELDS:
            _fail(path + "." + name, "reserved world authority or physical field; declare a separate program field")
        _mapping(spec, path + "." + name, {"type", "default", "min", "max"}, {"type", "default"})
        kind = spec["type"]
        current = {"type": kind, "default": spec["default"]}
        if kind == "boolean":
            _boolean(spec["default"], path + "." + name + ".default")
            if "min" in spec or "max" in spec:
                _fail(path + "." + name, "boolean fields cannot have min or max")
        elif kind == "integer":
            minimum = _integer(spec.get("min", -MAX_INTEGER), path + "." + name + ".min", -MAX_INTEGER, MAX_INTEGER)
            maximum = _integer(spec.get("max", MAX_INTEGER), path + "." + name + ".max", -MAX_INTEGER, MAX_INTEGER)
            if minimum > maximum:
                _fail(path + "." + name, "min must be less than or equal to max")
            _integer(spec["default"], path + "." + name + ".default", minimum, maximum)
            current.update(min=minimum, max=maximum)
        else:
            _fail(path + "." + name + ".type", "must be integer or boolean")
        result[name] = current
    return result


def _expression(expr, fields, params, materials, path, expected=None, object_ids=None):
    """Validate and infer one expression; string literals support event matching."""
    count = 0
    if object_ids is None:
        object_ids = set()

    def visit(value, at, depth):
        nonlocal count
        count += 1
        if count > MAX_EXPRESSION_NODES or depth > MAX_EXPRESSION_DEPTH:
            _fail(at, "expression exceeds 64 nodes or depth 8")
        if type(value) is bool:
            return "boolean"
        if type(value) is int:
            _integer(value, at, -MAX_INTEGER, MAX_INTEGER)
            return "integer"
        if isinstance(value, str):
            if len(value) > 160:
                _fail(at, "string literal exceeds 160 characters")
            return "string"
        _mapping(value, at)
        if len(value) == 1:
            key = next(iter(value))
            name = value[key]
            if key == "object":
                binding = _mapping(name, at + ".object", {"id", "field"}, {"id", "field"})
                entity_id = _text(binding["id"], at + ".object.id", 128)
                public_field = binding["field"]
                if not isinstance(public_field, str) or public_field not in _OBJECT_FIELDS:
                    _fail(at + ".object.field", "must reference a supported public object field")
                object_ids.add(entity_id)
                if len(object_ids) > 8:
                    _fail(at + ".object.id", "at most 8 distinct object bindings are allowed per program")
                return _OBJECT_FIELDS[public_field]
            if key in {"field", "param", "quantity", "concentration", "mechanical", "event", "contract"}:
                if not isinstance(name, str):
                    _fail(at, "reference name must be a string")
                table = {"field": fields, "param": params, "quantity": materials,
                         "concentration": materials, "mechanical": _MECHANICAL_FIELDS,
                         "event": _EVENT_FIELDS, "contract": _CONTRACT_FIELDS}[key]
                if name not in table:
                    _fail(at, f"undeclared {key} reference {name!r}")
                if key in {"field", "param"}:
                    return table[name]["type"]
                return table[name] if key in {"mechanical", "event", "contract"} else "integer"
        op = value.get("op")
        if isinstance(op, str) and op in _BINARY:
            _mapping(value, at, {"op", "left", "right"}, {"op", "left", "right"})
            left = visit(value["left"], at + ".left", depth + 1)
            right = visit(value["right"], at + ".right", depth + 1)
            if op in {"eq", "ne"}:
                if left != right:
                    _fail(at, "eq/ne operands must have the same type")
                return "boolean"
            if left != "integer" or right != "integer":
                _fail(at, "ordered comparisons and arithmetic require integer operands")
            return "boolean" if op in {"lt", "lte", "gt", "gte"} else "integer"
        if isinstance(op, str) and op in {"all", "any"}:
            _mapping(value, at, {"op", "args"}, {"op", "args"})
            args = _list(value["args"], at + ".args", MAX_EXPRESSION_NODES)
            if not args:
                _fail(at + ".args", "must contain at least one boolean expression")
            for index, child in enumerate(args):
                if visit(child, f"{at}.args[{index}]", depth + 1) != "boolean":
                    _fail(at, "all/any operands must be boolean")
            return "boolean"
        if op == "not":
            _mapping(value, at, {"op", "value"}, {"op", "value"})
            if visit(value["value"], at + ".value", depth + 1) != "boolean":
                _fail(at, "not operand must be boolean")
            return "boolean"
        _fail(at, "unknown expression; use a declared reference or a supported operator")

    kind = visit(expr, path, 1)
    if expected is not None and kind != expected:
        _fail(path, f"must evaluate to {expected}, got {kind}")
    return deepcopy(expr)


def _stoichiometry(value, materials, path):
    _mapping(value, path)
    if not value or len(value) > 8:
        _fail(path, "must contain from 1 to 8 material coefficients")
    for material, amount in value.items():
        if material not in materials:
            _fail(path + "." + material, "material is not declared or built in")
        _integer(amount, path + "." + material, 1, 100)
    return deepcopy(value)


def _reaction(effect, materials, path):
    _mapping(effect, path, {"op", "inputs", "outputs", "rate", "heat", "channel", "mode"}, {"op", "inputs", "outputs"})
    if effect["op"] != "react":
        _fail(path + ".op", "must be react")
    inputs = _stoichiometry(effect["inputs"], materials, path + ".inputs")
    outputs = _stoichiometry(effect["outputs"], materials, path + ".outputs")
    incoming = sum(materials[key]["load_per_u"] * amount for key, amount in inputs.items())
    outgoing = sum(materials[key]["load_per_u"] * amount for key, amount in outputs.items())
    if incoming != outgoing:
        detail = lambda group: ", ".join(f"{key}: {amount}u * load {materials[key]['load_per_u']}" for key, amount in sorted(group.items()))
        _fail(path, f"reaction mass must balance: inputs weigh {incoming} ({detail(inputs)}), outputs weigh {outgoing} ({detail(outputs)}); adjust coefficients or choose materials with appropriate unit loads. Local load_per_u must equal its feedstock; imported and builtin loads are fixed.")
    mode = effect.get("mode", "exclusive")
    if not isinstance(mode, str) or mode not in {"exclusive", "independent"}:
        _fail(path + ".mode", "must be exclusive or independent")
    return {"op": "react", "inputs": inputs, "outputs": outputs,
            "rate": _integer(effect.get("rate", 1), path + ".rate", 1, 10),
            "heat": _integer(effect.get("heat", 0), path + ".heat", -20, 20),
            "channel": _text(effect.get("channel", "chemistry"), path + ".channel", 80, _KEY), "mode": mode}


def _effects(effects, fields, params, materials, path, object_ids):
    result = []
    for index, effect in enumerate(_list(effects, path, 8)):
        at = f"{path}[{index}]"
        _mapping(effect, at)
        op = effect.get("op")
        if op == "set":
            _mapping(effect, at, {"op", "field", "value"}, {"op", "field", "value"})
            name = effect["field"]
            if not isinstance(name, str) or name not in fields:
                _fail(at + ".field", "set may only write a declared program field")
            result.append({"op": "set", "field": name, "value": _expression(effect["value"], fields, params, materials, at + ".value", fields[name]["type"], object_ids)})
        elif op == "react":
            result.append(_reaction(effect, materials, at))
        elif op == "emit":
            _mapping(effect, at, {"op", "name"}, {"op", "name"})
            result.append({"op": "emit", "name": _text(effect["name"], at + ".name", 80)})
        else:
            _fail(at + ".op", "must be set, react or emit")
    return result


def _material_catalog(materials):
    """Read the caller's complete built-in physics, never a second material table."""
    result = {}
    for key, definition in _mapping(materials, "materials").items():
        raw = definition.snapshot() if isinstance(definition, MaterialDef) else _mapping(definition, "materials." + key)
        result[key] = {"load_per_u": _integer(raw.get("load_per_u"), "materials." + key + ".load_per_u", 1, 8),
                       "combustible": _boolean(raw.get("combustible"), "materials." + key + ".combustible"),
                       "liquid": _boolean(raw.get("liquid"), "materials." + key + ".liquid")}
    return result


def _choices(values):
    return ", ".join(values[:-1]) + " or " + values[-1] if len(values) > 1 else values[0]


def compile_program(definition, *, materials, body_materials, max_custom_materials, imported_materials=None):
    """Return a validated canonical deep copy; invalid declarations raise ValueError."""
    _mapping(definition, "program", {"key", "name", "version", "materials", "material_imports", "types", "contact_reactions"}, {"key", "name", "version", "types"})
    result = {"key": _text(definition["key"], "program.key", 80, _KEY),
              "name": _text(definition["name"], "program.name"),
              "version": _integer(definition["version"], "program.version", 1, MAX_INTEGER),
              "materials": {}, "types": {}}
    object_ids = set()
    declared = _mapping(definition.get("materials", {}), "program.materials")
    imports = _mapping(definition.get("material_imports", {}), "program.material_imports")
    limit = _integer(max_custom_materials, "max_custom_materials", 0, MAX_INTEGER)
    if len(declared) + len(imports) > limit:
        _fail("program.materials", f"at most {limit} local and imported materials combined are allowed")
    builtin_materials = _material_catalog(materials)
    if not builtin_materials:
        _fail("materials", "at least one built-in material is required")
    if (not isinstance(body_materials, (list, tuple)) or not body_materials
            or any(not isinstance(key, str) or key not in builtin_materials for key in body_materials)
            or len(set(body_materials)) != len(body_materials)):
        _fail("body_materials", "must name distinct built-in body feedstocks")
    materials = deepcopy(builtin_materials)
    for symbol, spec in declared.items():
        at = "program.materials." + symbol
        _text(symbol, at, 48, _SYMBOL)
        if symbol in builtin_materials:
            _fail(at, "built-in materials cannot be redefined; choose a new symbol")
        _mapping(spec, at, {"name", "feedstock", "load_per_u", "combustible", "liquid"}, {"name", "feedstock", "load_per_u", "combustible", "liquid"})
        if not isinstance(spec["feedstock"], str) or spec["feedstock"] not in builtin_materials:
            _fail(at + ".feedstock", "must be " + _choices(list(builtin_materials)))
        name = _text(spec["name"], at + ".name", 80)
        if not name.isprintable():
            _fail(at + ".name", "material names cannot contain control characters")
        canonical = {"name": name, "feedstock": spec["feedstock"],
                     "load_per_u": _integer(spec["load_per_u"], at + ".load_per_u", 1, 8),
                     "combustible": _boolean(spec["combustible"], at + ".combustible"),
                     "liquid": _boolean(spec["liquid"], at + ".liquid")}
        if canonical["load_per_u"] != builtin_materials[canonical["feedstock"]]["load_per_u"]:
            required = builtin_materials[canonical["feedstock"]]["load_per_u"]
            _fail(at + ".load_per_u", f"must equal feedstock {canonical['feedstock']} load_per_u {required}, received {canonical['load_per_u']}; preparation converts one feedstock u into one local material u")
        result["materials"][symbol] = canonical
        materials[symbol] = canonical
    if imports:
        result["material_imports"] = {}
        references = set()
        for symbol, binding in imports.items():
            at = "program.material_imports." + symbol
            _text(symbol, at, 48, _SYMBOL)
            if symbol in materials:
                _fail(at, "import aliases cannot replace built-in or local materials")
            _mapping(binding, at, {"definition_ref", "symbol"}, {"definition_ref", "symbol"})
            reference = _text(binding["definition_ref"], at + ".definition_ref", 128)
            source_symbol = _text(binding["symbol"], at + ".symbol", 48, _SYMBOL)
            identity = reference + "#" + source_symbol
            if identity in references:
                _fail(at, "one immutable material identity cannot have multiple aliases in a program")
            references.add(identity)
            if imported_materials is None or symbol not in imported_materials:
                _fail(at, "requires an existing immutable published material reference")
            source = imported_materials[symbol]
            _mapping(source, at + ".resolved", {"name", "feedstock", "load_per_u", "combustible", "liquid"},
                     {"name", "load_per_u", "combustible", "liquid"})
            physical = {"name": _text(source["name"], at + ".resolved.name", 80),
                        "load_per_u": _integer(source["load_per_u"], at + ".resolved.load_per_u", 1, 8),
                        "combustible": _boolean(source["combustible"], at + ".resolved.combustible"),
                        "liquid": _boolean(source["liquid"], at + ".resolved.liquid")}
            result["material_imports"][symbol] = {"definition_ref": reference, "symbol": source_symbol}
            materials[symbol] = physical
    types = _mapping(definition["types"], "program.types")
    if not 1 <= len(types) <= 8:
        _fail("program.types", "must contain from 1 to 8 types")
    for symbol, spec in types.items():
        at = "program.types." + symbol
        _text(symbol, at, 48, _SYMBOL)
        _mapping(spec, at, {"name", "body_material", "cost", "fields", "container_capacity", "mechanical", "recorder", "contract", "barrier", "rules", "actions"}, {"name", "body_material", "cost"})
        if not isinstance(spec["body_material"], str) or spec["body_material"] not in body_materials:
            _fail(at + ".body_material", "must be " + _choices(body_materials))
        cost = _mapping(spec["cost"], at + ".cost", body_materials)
        canonical = {"name": _text(spec["name"], at + ".name"), "body_material": spec["body_material"],
                     "cost": {key: _integer(cost.get(key, 0), at + ".cost." + key, 0, 4) for key in body_materials},
                     "fields": _fields(spec.get("fields", {}), at + ".fields"), "rules": [], "actions": []}
        # Every body must consume real feedstock. The world converts pieces to lots.
        if canonical["cost"][spec["body_material"]] < 1:
            _fail(at + ".cost", "body_material must cost at least one real feedstock piece")
        fields = canonical["fields"]
        if "container_capacity" in spec:
            canonical["container_capacity"] = _integer(spec["container_capacity"], at + ".container_capacity", 1, 200)
        if "mechanical" in spec:
            mechanical = _mapping(spec["mechanical"], at + ".mechanical", {"role", "load_field", "speed_field"} | set(_MECHANICAL_DEFAULTS), {"role"})
            if not isinstance(mechanical["role"], str) or mechanical["role"] not in {"source", "gear", "actuator"}:
                _fail(at + ".mechanical.role", "must be source, gear or actuator")
            canonical["mechanical"] = {"role": mechanical["role"]}
            for key, default in _MECHANICAL_DEFAULTS.items():
                canonical["mechanical"][key] = _integer(mechanical.get(key, default), at + ".mechanical." + key, *_MECHANICAL_RANGES[key])
            for reference, minimum, maximum in (("load_field", 0, 100), ("speed_field", 1, 10)):
                if reference in mechanical:
                    field = mechanical[reference]
                    if not isinstance(field, str) or field not in fields or fields[field]["type"] != "integer":
                        _fail(at + ".mechanical." + reference, "must reference a declared integer field")
                    if fields[field]["min"] < minimum or fields[field]["max"] > maximum:
                        _fail(at + ".mechanical." + reference, f"referenced field min/max must stay within {minimum}..{maximum}")
                    canonical["mechanical"][reference] = field
        if "recorder" in spec:
            recorder = _mapping(spec["recorder"], at + ".recorder", {"radius", "window_ticks"}, {"radius", "window_ticks"})
            canonical["recorder"] = {"radius": _integer(recorder["radius"], at + ".recorder.radius", 0, 8),
                                     "window_ticks": _integer(recorder["window_ticks"], at + ".recorder.window_ticks", 1, 3600)}
        if "contract" in spec:
            contract = _mapping(spec["contract"], at + ".contract", {"material", "quantity", "duration_ticks", "completion_when", "delivery_when"}, {"material", "quantity", "duration_ticks"})
            if not isinstance(contract["material"], str) or contract["material"] not in materials:
                _fail(at + ".contract.material", "material is not declared or built in")
            canonical["contract"] = {"material": contract["material"], "quantity": _integer(contract["quantity"], at + ".contract.quantity", 1, 100),
                                     "duration_ticks": _integer(contract["duration_ticks"], at + ".contract.duration_ticks", 1, 3600)}
            for condition in ("completion_when", "delivery_when"):
                if condition in contract:
                    canonical["contract"][condition] = _expression(contract[condition], fields, {}, materials, at + ".contract." + condition, "boolean", object_ids)
        if "barrier" in spec:
            canonical["barrier"] = _expression(spec["barrier"], fields, {}, materials, at + ".barrier", "boolean", object_ids)
        rule_ids = set()
        for index, rule in enumerate(_list(spec.get("rules", []), at + ".rules", 8)):
            rp = f"{at}.rules[{index}]"
            _mapping(rule, rp, {"id", "phase", "when", "effects"}, {"id", "phase", "when", "effects"})
            rid = _text(rule["id"], rp + ".id", 80, _KEY)
            if rid in rule_ids:
                _fail(rp + ".id", "duplicate rule id within this type")
            rule_ids.add(rid)
            if not isinstance(rule["phase"], str) or rule["phase"] not in {"reaction", "after"}:
                _fail(rp + ".phase", "must be reaction or after")
            canonical["rules"].append({"id": rid, "phase": rule["phase"],
                                       "when": _expression(rule["when"], fields, {}, materials, rp + ".when", "boolean", object_ids),
                                       "effects": _effects(rule["effects"], fields, {}, materials, rp + ".effects", object_ids)})
        action_ids = set()
        for index, action in enumerate(_list(spec.get("actions", []), at + ".actions", 8)):
            ap = f"{at}.actions[{index}]"
            _mapping(action, ap, {"id", "label", "parameters", "effects", "when"}, {"id", "label", "effects"})
            aid = _text(action["id"], ap + ".id", 80, _KEY)
            if aid in _RESERVED_ACTIONS:
                _fail(ap + ".id", "reserved consent or world action; choose a distinct authored action id")
            if aid in action_ids:
                _fail(ap + ".id", "duplicate action id within this type")
            action_ids.add(aid)
            params = _fields(action.get("parameters", {}), ap + ".parameters")
            compiled = {"id": aid, "label": _text(action["label"], ap + ".label"), "parameters": params,
                        "effects": _effects(action["effects"], fields, params, materials, ap + ".effects", object_ids)}
            if "when" in action:
                compiled["when"] = _expression(action["when"], fields, params, materials, ap + ".when", "boolean", object_ids)
            canonical["actions"].append(compiled)
        result["types"][symbol] = canonical
    if "contact_reactions" in definition:
        result["contact_reactions"] = []
        reaction_ids = set()
        for index, rule in enumerate(_list(definition["contact_reactions"], "program.contact_reactions", 8)):
            at = f"program.contact_reactions[{index}]"
            _mapping(rule, at, {"id", "when", "reaction"}, {"id", "when", "reaction"})
            rid = _text(rule["id"], at + ".id", 80, _KEY)
            if rid in reaction_ids:
                _fail(at + ".id", "duplicate contact reaction id")
            reaction_ids.add(rid)
            condition = _expression(rule["when"], {}, {}, materials, at + ".when", "boolean", set())
            def check_refs(value):
                if isinstance(value, dict):
                    if set(value).intersection({"field", "param", "mechanical", "event", "contract", "object"}):
                        _fail(at + ".when", "contact conditions only read local quantities and concentrations")
                    for child in value.values():
                        check_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        check_refs(child)
            check_refs(condition)
            effect = _reaction(rule["reaction"], materials, at + ".reaction")
            if not set(effect["inputs"]).intersection(result["materials"]):
                _fail(at + ".reaction.inputs", "contact must consume at least one material locally declared by this program")
            if effect["heat"]:
                _fail(at + ".reaction.heat", "ordinary containers have no temperature field; contact heat must be zero")
            result["contact_reactions"].append({"id": rid, "when": condition, "reaction": effect})
    return result


def program_material_semantics(definition, definition_ref, *, materials, type_id=None):
    """Project compiled material bindings and coefficients without executing them.

    The world has already resolved imports to their original immutable reference.
    This projection stays outside the stored definition and its content hash.
    Names and author descriptions play no part in material identity resolution.
    """
    types = definition["types"] if type_id is None else {type_id: definition["types"][type_id]}
    builtin_materials = _mapping(materials, "materials")
    symbols = set(definition["materials"]) | set(definition.get("material_imports", {}))
    projected_types = {}
    for symbol, kind in types.items():
        reactions = []
        for group in ("rules", "actions"):
            for declaration in kind[group]:
                for index, effect in enumerate(declaration["effects"]):
                    if effect["op"] != "react":
                        continue
                    symbols.update(effect["inputs"])
                    symbols.update(effect["outputs"])
                    origin = ({"rule_id": declaration["id"], "phase": declaration["phase"]}
                              if group == "rules" else {"action_id": declaration["id"]})
                    reactions.append({**origin, "effect_index": index,
                        "inputs_per_extent": deepcopy(effect["inputs"]),
                        "outputs_per_extent": deepcopy(effect["outputs"]),
                        "max_extent": effect["rate"], "channel": effect["channel"], "mode": effect["mode"]})
        current = {"reactions": reactions}
        if "contract" in kind:
            contract = kind["contract"]
            symbols.add(contract["material"])
            current["delivery"] = {"material": contract["material"], "quantity_u": contract["quantity"]}
        projected_types[symbol] = current

    contacts = []
    for rule in definition.get("contact_reactions", []):
        effect = rule["reaction"]
        symbols.update(effect["inputs"])
        symbols.update(effect["outputs"])
        contacts.append({"rule_id": rule["id"], "when": deepcopy(rule["when"]),
            "inputs_per_extent": deepcopy(effect["inputs"]), "outputs_per_extent": deepcopy(effect["outputs"]),
            "max_extent": effect["rate"], "channel": effect["channel"], "mode": effect["mode"]})
    preparations = {}
    for symbol, material in definition["materials"].items():
        symbols.add(material["feedstock"])
        preparations[symbol] = {"inputs_per_unit": {material["feedstock"]: 1},
                                "outputs_per_unit": {symbol: 1}}
    bindings = {}
    for symbol in sorted(symbols):
        if symbol in builtin_materials:
            bindings[symbol] = {"kind": "builtin", "material_id": symbol}
        else:
            source = definition.get("material_imports", {}).get(symbol)
            reference = source["definition_ref"] if source else definition_ref
            original_symbol = source["symbol"] if source else symbol
            bindings[symbol] = {"kind": "imported" if source else "local",
                "material_id": reference + "#" + original_symbol,
                "definition_ref": reference, "symbol": original_symbol}
    return {"scope": "compiled_material_semantics", "definition_ref": definition_ref,
        "unit": "logical_matter", "materials": bindings, "types": projected_types,
        "local_preparation": preparations, "contact_reactions": contacts,
        "meaning": "Symbols in consumption and production map to materials[symbol].material_id, never to display names. "
            "Reaction coefficients are per extent, not executed quantities; actual conditions, input lots, capacity "
            "and competing effects determine execution. local_preparation is charge's one-to-one feedstock conversion, "
            "not use of an existing foreign lot or proof of a reaction. Imports supply neither matter nor permission. "
            "Delivery quantities describe terms, not acceptance or completed delivery."}


def evaluate(expr, context):
    """Evaluate bounded data against explicit state and public fact context.

    Concentration is the integer percentage of simplified volume shares (u),
    not a mass fraction or physical concentration. Division
    is integer floor division. Missing quantities are zero; other missing facts
    are errors. Mechanical fractions are truncated toward zero for expressions.
    """
    _mapping(context, "context")
    count = 0

    def public_fact(current, kind, reference):
        if type(current) is not {"boolean": bool, "integer": int, "string": str}[kind]:
            _fail("expression", f"{reference} fact must be {kind}")
        return current

    def visit(value, depth):
        nonlocal count
        count += 1
        if count > MAX_EXPRESSION_NODES or depth > MAX_EXPRESSION_DEPTH:
            _fail("expression", "expression exceeds 64 nodes or depth 8")
        if type(value) in {bool, int, str}:
            if type(value) is int:
                _integer(value, "expression", -MAX_INTEGER, MAX_INTEGER)
            elif isinstance(value, str) and len(value) > 160:
                _fail("expression", "string literal exceeds 160 characters")
            return value
        _mapping(value, "expression")
        if len(value) == 1:
            ref = next(iter(value))
            name = value[ref]
            if ref == "object":
                binding = _mapping(name, "expression.object", {"id", "field"}, {"id", "field"})
                entity_id = _text(binding["id"], "expression.object.id", 128)
                public_field = binding["field"]
                if not isinstance(public_field, str) or public_field not in _OBJECT_FIELDS:
                    _fail("expression.object.field", "must reference a supported public object field")
                objects = _mapping(context.get("objects", {}), "context.objects")
                if entity_id not in objects:
                    _fail("expression", f"missing public object fact {entity_id!r}.{public_field}")
                source = _mapping(objects[entity_id], "context.objects." + entity_id)
                if public_field not in source:
                    _fail("expression", f"missing public object fact {entity_id!r}.{public_field}")
                return public_fact(source[public_field], _OBJECT_FIELDS[public_field], f"object {entity_id!r}.{public_field}")
            if ref in {"field", "param", "quantity", "concentration", "mechanical", "event", "contract"}:
                if not isinstance(name, str):
                    _fail("expression", "reference name must be a string")
                group = {"field": "fields", "param": "params", "quantity": "quantities", "concentration": "quantities", "mechanical": "mechanical", "event": "event", "contract": "contract"}[ref]
                source = _mapping(context.get(group, {}), "context." + group)
                if ref in {"quantity", "concentration"}:
                    for key, amount in source.items():
                        _integer(amount, "context.quantities." + key, 0, MAX_INTEGER)
                    amount = source.get(name, 0)
                    return amount if ref == "quantity" else amount * 100 // max(1, sum(source.values()))
                public_fields = {"mechanical": _MECHANICAL_FIELDS, "event": _EVENT_FIELDS, "contract": _CONTRACT_FIELDS}.get(ref)
                if public_fields is not None and name not in public_fields:
                    _fail("expression", "reference is not a public mechanical/event/contract field")
                if name not in source:
                    _fail("expression", f"missing {ref} fact {name!r}")
                current = source[name]
                if ref == "mechanical" and name != "status":
                    current = int(_fraction(current, "context.mechanical." + name))
                if public_fields is not None:
                    return public_fact(current, public_fields[name], f"{ref} {name!r}")
                if type(current) not in {bool, int, str}:
                    _fail("expression", f"{ref} fact must be boolean, integer or string")
                return current
        op = value.get("op")
        if isinstance(op, str) and op in _BINARY:
            _mapping(value, "expression", {"op", "left", "right"}, {"op", "left", "right"})
            left, right = visit(value["left"], depth + 1), visit(value["right"], depth + 1)
            if op in {"eq", "ne"}:
                if type(left) is not type(right):
                    _fail("expression", "eq/ne operands must have the same type")
                return left == right if op == "eq" else left != right
            if type(left) is not int or type(right) is not int:
                _fail("expression", "arithmetic and ordered comparisons require integer operands")
            if op == "lt":
                return left < right
            if op == "lte":
                return left <= right
            if op == "gt":
                return left > right
            if op == "gte":
                return left >= right
            if op == "add":
                return left + right
            if op == "sub":
                return left - right
            if op == "mul":
                return left * right
            if op == "div":
                if right == 0:
                    _fail("expression", "division by zero; add a nonzero guard or use a positive divisor")
                return left // right
            return min(left, right) if op == "min" else max(left, right)
        if isinstance(op, str) and op in {"all", "any"}:
            _mapping(value, "expression", {"op", "args"}, {"op", "args"})
            args = _list(value["args"], "expression.args", MAX_EXPRESSION_NODES)
            if not args:
                _fail("expression.args", "must contain a boolean expression")
            for child in args:
                current = visit(child, depth + 1)
                if type(current) is not bool:
                    _fail("expression", "all/any operands must be boolean")
                if op == "all" and not current:
                    return False
                if op == "any" and current:
                    return True
            return op == "all"
        if op == "not":
            _mapping(value, "expression", {"op", "value"}, {"op", "value"})
            current = visit(value["value"], depth + 1)
            if type(current) is not bool:
                _fail("expression", "not operand must be boolean")
            return not current
        _fail("expression", "unsupported expression")

    return visit(expr, 1)


def plan_reaction(effect, quantities, material_defs, capacity, *, liquid_capacity=None, liquid_held=0):
    """Plan one atomic integer extent without mutating any input.

    All inputs compete for the same extent. Capacity is total quantity in logical
    u, including unchanged contents. A caller must apply consume and produce
    atomically, and pass its updated allocation to the next independent channel.
    """
    _mapping(material_defs, "material_defs")
    materials = {}
    for key, spec in material_defs.items():
        load = spec.get("load_per_u") if isinstance(spec, dict) else spec.load_per_u if isinstance(spec, MaterialDef) else None
        materials[key] = {"load_per_u": _integer(load, "material_defs." + key + ".load_per_u", 1, 8)}
    canonical = _reaction(effect, materials, "reaction")
    _mapping(quantities, "quantities")
    for key, amount in quantities.items():
        if key not in materials:
            _fail("quantities." + key, "unknown material")
        _integer(amount, "quantities." + key, 0, MAX_INTEGER)
    if capacity is not None:
        _integer(capacity, "capacity", 0, MAX_INTEGER)
    if liquid_capacity is not None:
        _integer(liquid_capacity, "liquid_capacity", 0, MAX_INTEGER)
        _integer(liquid_held, "liquid_held", 0, MAX_INTEGER)
    extent = min([canonical["rate"]] + [quantities.get(key, 0) // amount for key, amount in canonical["inputs"].items()])
    reason = "ready"
    if extent < canonical["rate"]:
        reason = "insufficient_inputs"
    current = sum(quantities.values())
    growth = sum(canonical["outputs"].values()) - sum(canonical["inputs"].values())
    if capacity is not None:
        # Search only the at-most-ten candidate extents; a shrinking reaction may
        # legitimately repair an overfull vessel, but may not leave it overfull.
        permitted = next((candidate for candidate in range(extent, -1, -1)
                          if current + candidate * growth <= capacity), 0)
        if permitted < extent or current > capacity and permitted == 0:
            reason = "capacity"
        extent = permitted
    if liquid_capacity is not None:
        def is_liquid(key):
            spec = material_defs[key]
            return spec.get("liquid", False) if isinstance(spec, dict) else spec.liquid
        liquid_growth = sum(n for key, n in canonical["outputs"].items() if is_liquid(key)) - sum(
            n for key, n in canonical["inputs"].items() if is_liquid(key))
        permitted = next((candidate for candidate in range(extent, -1, -1)
                          if liquid_held + candidate * liquid_growth <= liquid_capacity), 0)
        if permitted < extent:
            reason = "liquid_capacity"
        extent = permitted
    return {"extent": extent,
            "consume": {key: amount * extent for key, amount in canonical["inputs"].items()} if extent else {},
            "produce": {key: amount * extent for key, amount in canonical["outputs"].items()} if extent else {},
            "heat": canonical["heat"] * extent, "reason": reason}


def _fraction(value, path):
    if type(value) not in {int, float, Fraction} or isinstance(value, float) and not math.isfinite(value):
        _fail(path, "must be a finite number")
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _number(value):
    return value.numerator if value.denominator == 1 else float(value)


def solve_mechanical(nodes, edges):
    """Solve one finite network step, preserving old positions on every stall.

    Nodes may be an id->node mapping or a list with explicit ids. Each node has
    mechanical parameters (nested under ``mechanical`` or flat), and current
    ``position``, ``angle`` and finite source ``energy`` (in ``state`` or flat).
    Edges have source/target, positive numerator/denominator, reversed and an
    optional meshed flag. Meshed teeth derive the ratio and reverse direction.
    The result records energy_used only at a source, once per connected graph.
    Optional load_field/speed_field resolve against node.fields or state.fields;
    their bounded values override the respective mechanical input parameters.
    """
    if isinstance(nodes, dict):
        raw_nodes = [(key, value) for key, value in nodes.items()]
    elif isinstance(nodes, list):
        raw_nodes = [(value.get("id") if isinstance(value, dict) else None, value) for value in nodes]
    else:
        _fail("nodes", "must be an id-to-node object or a node list")
    if len(raw_nodes) > 64:
        _fail("nodes", "at most 64 mechanical nodes are allowed")
    canonical, result = {}, {}
    for key, node in raw_nodes:
        _text(key, "node.id", 180)
        if key in canonical:
            _fail("nodes", "duplicate node id " + key)
        _mapping(node, "nodes." + key)
        spec = _mapping(node.get("mechanical", node), "nodes." + key + ".mechanical")
        state = _mapping(node.get("state", node), "nodes." + key + ".state")
        if not isinstance(spec.get("role"), str) or spec.get("role") not in {"source", "gear", "actuator"}:
            _fail("nodes." + key + ".role", "must be source, gear or actuator")
        current = {"role": spec["role"]}
        for field, default in _MECHANICAL_DEFAULTS.items():
            current[field] = _integer(spec.get(field, default), "nodes." + key + "." + field, *_MECHANICAL_RANGES[field])
        fields = _mapping(node.get("fields", state.get("fields", {})), "nodes." + key + ".fields")
        for reference, target in (("load_field", "load"), ("speed_field", "speed")):
            if reference in spec:
                name = spec[reference]
                if not isinstance(name, str) or name not in fields:
                    _fail("nodes." + key + "." + reference, "missing referenced integer field")
                current[target] = _integer(fields[name], "nodes." + key + ".fields." + name, *_MECHANICAL_RANGES[target])
        for field in ("position", "angle"):
            current[field] = _fraction(state.get(field, node.get(field, 0)), "nodes." + key + "." + field)
        current["energy"] = _integer(state.get("energy", node.get("energy", 0)), "nodes." + key + ".energy", 0, MAX_INTEGER)
        canonical[key] = current
        result[key] = {"position": _number(current["position"]), "angle": _number(current["angle"]),
                       "speed": 0, "status": "no_source", "energy_used": 0, "available_torque": 0}
    graph = {key: [] for key in canonical}
    for index, edge in enumerate(_list(edges, "edges", 128)):
        at = f"edges[{index}]"
        _mapping(edge, at, {"source", "target", "numerator", "denominator", "reversed", "meshed"}, {"source", "target"})
        source, target = edge["source"], edge["target"]
        if not isinstance(source, str) or not isinstance(target, str) or source not in canonical or target not in canonical:
            _fail(at, "source and target must reference existing mechanical nodes")
        numerator = _integer(edge.get("numerator", 1), at + ".numerator", 1, 100)
        denominator = _integer(edge.get("denominator", 1), at + ".denominator", 1, 100)
        reversed_direction = _boolean(edge.get("reversed", False), at + ".reversed")
        meshed = _boolean(edge.get("meshed", False), at + ".meshed")
        ratio = Fraction(-canonical[source]["teeth"], canonical[target]["teeth"]) if meshed else Fraction(numerator, denominator)
        if reversed_direction:
            ratio = -ratio
        graph[source].append((target, ratio))
        graph[target].append((source, 1 / ratio))
    visited = set()
    for start in canonical:
        if start in visited:
            continue
        ratios, pending, inconsistent = {start: Fraction(1)}, [start], False
        while pending:
            current = pending.pop()
            visited.add(current)
            for target, ratio in graph[current]:
                required = ratios[current] * ratio
                if target in ratios:
                    inconsistent |= ratios[target] != required
                else:
                    ratios[target] = required
                    pending.append(target)
        sources = [key for key in ratios if canonical[key]["role"] == "source"]
        status = "inconsistent_cycle" if inconsistent else "no_source" if not sources else "multiple_sources" if len(sources) != 1 else None
        if status is None:
            source = sources[0]
            origin = canonical[source]
            relative = {key: ratio / ratios[source] for key, ratio in ratios.items()}
            load = sum(Fraction(canonical[key]["load"]) * abs(ratio)
                       * (canonical[key]["pitch"] if canonical[key]["role"] == "actuator" else 1)
                       for key, ratio in relative.items())
            if origin["energy"] < origin["energy_per_step"]:
                status = "insufficient_energy"
            elif load > origin["torque"]:
                status = "overloaded"
            else:
                remaining = Fraction(origin["torque"]) - load
                for key, ratio in relative.items():
                    node = canonical[key]
                    turns = origin["speed"] * ratio
                    position = node["position"]
                    node_status = "running"
                    if node["role"] == "actuator":
                        proposed = position + turns * node["pitch"]
                        position = min(Fraction(node["travel"]), max(Fraction(0), proposed))
                        if position != proposed or position in (0, node["travel"]):
                            node_status = "travel_limit"
                    result[key] = {"position": _number(position), "angle": _number(node["angle"] + turns),
                                   "speed": _number(turns), "status": node_status,
                                   "energy_used": origin["energy_per_step"] if key == source else 0,
                                   "available_torque": _number(remaining / abs(ratio))}
        if status is not None:
            for key in ratios:
                result[key]["status"] = status
    return result
