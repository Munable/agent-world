"""Machine drafting grammar derived from the program compiler's vocabulary.

This schema is advice for composing a draft, never a replacement admission
validator. Compilation still resolves identities and types, checks conserved
mass and shared budgets, and returns the unchanged canonical definition.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from . import programs as p
from . import presentation as display
from .facts import FACT_TYPES

PROGRAM_SCHEMA_VERSION = 1
MAX_PROGRAM_SCHEMA_BYTES = 24 * 1024
PROGRAM_SCHEMA_NODES = (
    "definition", "types", "type", "fields", "field", "materials", "material",
    "material_imports", "material_import", "source", "ports", "port",
    "rule", "action", "expression", "condition", "effect", "reaction",
    "stoichiometry", "mechanical", "recorder", "contract", "components",
    "presentation", "contact_reaction",
)


_DEFINITION_NAMES = {
    "definition": "def", "types": "types", "type": "type", "fields": "fields", "field": "field",
    "materials": "mats", "material": "mat", "material_imports": "imports", "material_import": "import",
    "source": "src", "ports": "ports", "port": "port", "rule": "rule", "action": "act",
    "expression": "expr", "condition": "cond", "effect": "eff", "reaction": "react",
    "stoichiometry": "stoich", "mechanical": "mech", "recorder": "rec", "contract": "contract",
    "components": "comp", "presentation": "view", "contact_reaction": "contact",
    "symbol_name": "sym", "key_name": "key", "field_name": "name",
    "builtin_material_name": "builtin", "local_material_name": "local", "material_name": "material",
    "authored_fact_name": "auth", "public_fact_name": "pub", "port_fact_name": "portfact",
    "public_integer_fact_name": "ai", "public_boolean_fact_name": "ab", "public_string_fact_name": "as",
    "field_reference": "rf", "param_reference": "rp", "quantity_reference": "rq", "concentration_reference": "rc",
    "object_reference": "obj", "object_integer_reference": "obj_i", "object_boolean_reference": "obj_b",
    "expression_leaf": "atom", "boolean_leaf": "bool", "integer_leaf": "int",
    "binary_operator_form": "bin", "logical_operator_form": "log", "not_operator_form": "not",
    "boolean_operator_result": "bop", "arithmetic_operator_result": "iop",
    "state_expression": "state", "contact_expression": "ce", "contact_condition": "cc",
}


def _definition_key(name):
    """Compact internal identifiers; public node and property names stay intact."""
    if name in _DEFINITION_NAMES:
        return "p_" + _DEFINITION_NAMES[name]
    depth = re.fullmatch(r"expression_(\d+)", name)
    if depth:
        return "p_e" + depth[1]
    fact = re.fullmatch(r"(mechanical|event|contract|physical)(?:_(boolean|integer|string))?_(name|reference)", name)
    if fact:
        source = {"mechanical": "m", "event": "e", "contract": "c", "physical": "p"}[fact[1]]
        kind = {"boolean": "b", "integer": "i", "string": "s", None: ""}[fact[2]]
        return "p_" + source + kind + ("r" if fact[3] == "reference" else "")
    raise ValueError("Unmapped internal program schema name: " + name)


def _ref(name):
    return {"$ref": "#/$defs/" + _definition_key(name)}


def _enum(values):
    values = list(values)
    return {"enum": values} if values else False


def _object(properties, required=(), **extra):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False, **extra}


def _shape(name, properties, **extra):
    allowed, required = p.NODE_SHAPES[name]
    if set(properties) != set(allowed):
        raise ValueError("Drafting schema does not cover compiler keys: " + name)
    return _object(properties, required, **extra)


def _integer(minimum=-p.MAX_INTEGER, maximum=p.MAX_INTEGER, **extra):
    return {"type": "integer", "minimum": minimum, "maximum": maximum, **extra}


def _text(maximum=160, *, nonempty=True):
    result = {"type": "string", "maxLength": maximum}
    if nonempty:
        result.update(minLength=1, pattern=r"\S")
    return result


def _full_pattern(compiled):
    # Python's \Z is not portable JSON Schema regex syntax. This lookahead
    # retains full-match semantics even for an input ending in a newline.
    return "^(?:" + compiled.pattern.removesuffix(r"\Z") + r")(?![\s\S])"


def _array(items, maximum, *, minimum=0, unique=False):
    result = {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}
    if unique:
        result["uniqueItems"] = True
    return result


def _dictionary(value, names, *, maximum=None, minimum=0):
    result = {"type": "object", "propertyNames": names, "additionalProperties": value,
              "minProperties": minimum}
    if maximum is not None:
        result["maxProperties"] = maximum
    return result


def _definition_graph(*, materials, body_materials, max_custom_materials,
                      component_catalog=None, fact_catalog=None):
    # Use the same trusted registry acceptance as compile_program; no second
    # list of built-in material names or component properties is maintained.
    builtin = p._material_catalog(materials)
    if (not builtin or not isinstance(body_materials, (list, tuple)) or not body_materials
            or any(not isinstance(name, str) or name not in builtin for name in body_materials)
            or len(set(body_materials)) != len(body_materials)):
        raise ValueError("Drafting schema requires the compiler's actual built-in materials")
    if type(max_custom_materials) is not int or not 0 <= max_custom_materials <= p.MAX_INTEGER:
        raise ValueError("Invalid custom material limit")
    facts = dict(FACT_TYPES if fact_catalog is None else fact_catalog)
    if any(kind not in {"boolean", "integer", "string"} for kind in facts.values()):
        raise ValueError("Invalid trusted fact catalog")
    boolean = {"type": "boolean"}
    defs = {}

    def put(name, value):
        key = _definition_key(name)
        if key in defs:
            raise ValueError("Duplicate internal program schema name: " + name)
        defs[key] = value

    # Repeated catalogs/patterns are referenced, not serialized once per
    # occurrence or expression depth. Node pruning retains their exact sets.
    put("symbol_name", {"type": "string", "maxLength": 48, "pattern": _full_pattern(p._SYMBOL)})
    put("key_name", {"type": "string", "maxLength": 80, "pattern": _full_pattern(p._KEY)})
    put("field_name", {"allOf": [_ref("symbol_name"), {"not": {"enum": sorted(p._RESERVED_FIELDS)}}]})
    put("builtin_material_name", _enum(sorted(builtin)))
    put("local_material_name", {"allOf": [_ref("symbol_name"), {"not": _ref("builtin_material_name")}]})
    put("material_name", {"anyOf": [_ref("builtin_material_name"), _ref("symbol_name")]})
    put("authored_fact_name", {"type": "string", "pattern": r"^authored\.[a-z][a-z0-9_]{0,47}(?![\s\S])"})
    public_kinds = []
    for kind in ("integer", "boolean", "string"):
        name = "public_" + kind + "_fact_name"
        put(name, _enum(sorted(field for field, field_kind in facts.items() if field_kind == kind)))
        public_kinds.append(_ref(name))
    put("public_fact_name", {"anyOf": public_kinds})
    put("port_fact_name", {"anyOf": [_ref("public_fact_name"), _ref("authored_fact_name")]})
    symbol, key, field_name = _ref("symbol_name"), _ref("key_name"), _ref("field_name")
    local_material, material_name = _ref("local_material_name"), _ref("material_name")
    public_name = _ref("port_fact_name")
    put("field", {"oneOf": [
        _object({"type": {"const": "boolean"}, "default": boolean}, ("type", "default")),
        _shape("field", {"type": {"const": "integer"}, "default": _integer(),
            "min": _integer(), "max": _integer()})]})
    put("fields", _dictionary(_ref("field"), field_name, maximum=p.MAX_FIELDS))
    put("source", _shape("source", {"definition_ref": _text(128), "type_id": symbol}))
    put("material_import", _shape("material_import", {"definition_ref": _text(128), "symbol": symbol}))
    put("material_imports", _dictionary(_ref("material_import"), local_material, maximum=max_custom_materials))
    put("material", _shape("material", {"name": _text(80),
        "feedstock": _ref("builtin_material_name"), "load_per_u": _integer(1, 8),
        "combustible": boolean, "liquid": boolean,
        "uses": _object({use: boolean for use in p.MATERIAL_USES})}))
    put("materials", _dictionary(_ref("material"), local_material, maximum=max_custom_materials))
    put("port", _shape("port", {"label": _text(80),
        "fields": _array(public_name, p.PROGRAM_LIMITS["port_fields"], minimum=1, unique=True),
        "source": _ref("source")}))
    put("ports", _dictionary(_ref("port"), symbol, maximum=p.PROGRAM_LIMITS["ports"]))

    # Atoms name only formal references. Authored fields exist exclusively
    # through an explicitly pinned port, never an arbitrary fixed object id.
    references = []
    boolean_references, integer_references = [], []
    for name in ("field", "param", "quantity", "concentration"):
        atom = _object({name: material_name if name in {"quantity", "concentration"} else field_name}, (name,))
        put(name + "_reference", atom)
        reference = _ref(name + "_reference")
        references.append(reference)
        if name in {"field", "param"}:
            boolean_references.append(reference)
        integer_references.append(reference)
    tables = {"mechanical": p._MECHANICAL_FIELDS, "event": p._EVENT_FIELDS,
              "contract": p._CONTRACT_FIELDS,
              "physical": {name.removeprefix("physical."): kind for name, kind in facts.items()
                           if name.startswith("physical.")}}
    for name, table in tables.items():
        if not table:
            continue
        typed_names = []
        for kind in ("boolean", "integer", "string"):
            names = sorted(field for field, field_kind in table.items() if field_kind == kind)
            if names:
                catalog = name + "_" + kind + "_name"
                put(catalog, _enum(names))
                typed_names.append(_ref(catalog))
        put(name + "_reference", _object({name: {"anyOf": typed_names}}, (name,)))
        references.append(_ref(name + "_reference"))
        for kind, target in (("boolean", boolean_references), ("integer", integer_references)):
            if any(field_kind == kind for field_kind in table.values()):
                atom = name + "_" + kind + "_reference"
                put(atom, _object({name: _ref(name + "_" + kind + "_name")}, (name,)))
                target.append(_ref(atom))
    object_binding = {"oneOf": [
        _object({"id": _text(128), "field": _ref("public_fact_name")}, ("id", "field")),
        _object({"port": symbol, "field": public_name}, ("port", "field"))]}
    put("object_reference", _object({"object": object_binding}, ("object",)))
    references.append(_ref("object_reference"))
    for kind, target in (("boolean", boolean_references), ("integer", integer_references)):
        typed_public = {"anyOf": [_ref("public_" + kind + "_fact_name"), _ref("authored_fact_name")]}
        atom = "object_" + kind + "_reference"
        put(atom, _object({"object": {"oneOf": [
            _object({"id": _text(128), "field": _ref("public_" + kind + "_fact_name")}, ("id", "field")),
            _object({"port": symbol, "field": typed_public}, ("port", "field"))]}}, ("object",)))
        target.append(_ref(atom))
    put("expression_leaf", {"anyOf": [boolean, _integer(), _text(nonempty=False), *references]})
    put("boolean_leaf", {"anyOf": [boolean, *boolean_references]})
    put("integer_leaf", {"anyOf": [_integer(), *integer_references]})
    put("binary_operator_form", _object({"op": {"enum": sorted(p._BINARY)}, "left": True, "right": True},
        ("op", "left", "right")))
    put("logical_operator_form", _object({"op": {"enum": ["all", "any"]},
        "args": _array(True, p.MAX_EXPRESSION_NODES, minimum=1)}, ("op", "args")))
    put("not_operator_form", _object({"op": {"const": "not"}, "value": True}, ("op", "value")))

    def operators(child):
        return [
            {"allOf": [_ref("binary_operator_form"), {"properties": {"left": child, "right": child}}]},
            {"allOf": [_ref("logical_operator_form"), {"properties": {"args": {"items": child}}}]},
            {"allOf": [_ref("not_operator_form"), {"properties": {"value": child}}]}]

    for depth in range(p.MAX_EXPRESSION_DEPTH, 0, -1):
        alternatives = [_ref("expression_leaf")]
        if depth < p.MAX_EXPRESSION_DEPTH:
            child = _ref("expression_" + str(depth + 1))
            alternatives.extend(operators(child))
        put("expression_" + str(depth), {"anyOf": alternatives})
    put("expression", _ref("expression_1"))
    boolean_operator = _object({"op": {"enum": ["eq", "ne", "lt", "lte", "gt", "gte", "all", "any", "not"]}},
        ("op",))
    boolean_operator.pop("additionalProperties")
    arithmetic_operator = _object({"op": {"enum": sorted(p._BINARY - {"eq", "ne", "lt", "lte", "gt", "gte"})}}, ("op",))
    arithmetic_operator.pop("additionalProperties")
    put("boolean_operator_result", boolean_operator)
    put("arithmetic_operator_result", arithmetic_operator)
    put("condition", {"allOf": [_ref("expression"), {"anyOf": [_ref("boolean_leaf"), _ref("boolean_operator_result")]}]})
    put("state_expression", {"allOf": [_ref("expression"),
        {"anyOf": [_ref("boolean_leaf"), _ref("integer_leaf"), _ref("boolean_operator_result"), _ref("arithmetic_operator_result")]}]})
    # Contact predicates use the same expression language but cannot name
    # object, program-state, event, mechanical or physical references.
    contact = _ref("contact_expression")
    put("contact_expression", {"anyOf": [boolean, _integer(), _text(nonempty=False),
        _ref("quantity_reference"), _ref("concentration_reference"), *operators(contact)]})
    put("contact_condition", {"allOf": [_ref("condition"), contact]})

    put("stoichiometry", _dictionary(_integer(*p.REACTION_RANGES["coefficient"]), material_name,
        maximum=p.PROGRAM_LIMITS["material_coefficients"], minimum=1))
    put("reaction", _shape("reaction", {"op": {"const": "react"},
        "inputs": _ref("stoichiometry"), "outputs": _ref("stoichiometry"),
        "rate": _integer(*p.REACTION_RANGES["rate"]), "heat": _integer(*p.REACTION_RANGES["heat"]),
        "channel": key, "mode": {"enum": ["exclusive", "independent"]}}))
    put("effect", {"oneOf": [
        _shape("set_effect", {"op": {"const": "set"}, "field": field_name, "value": _ref("state_expression")}),
        _shape("drive_effect", {"op": {"const": "drive"}, "enabled": _ref("condition")}),
        _ref("reaction"), _shape("emit_effect", {"op": {"const": "emit"}, "name": _text(80)})]})
    effects = _array(_ref("effect"), p.PROGRAM_LIMITS["effects"])
    put("rule", _shape("rule", {"id": key, "phase": {"enum": ["reaction", "after"]},
        "when": _ref("condition"), "effects": effects}))
    put("action", _shape("action", {"id": {"allOf": [key, {"not": {"enum": sorted(p._RESERVED_ACTIONS)}}]},
        "label": _text(), "parameters": _ref("fields"), "effects": effects, "when": _ref("condition")}))
    put("mechanical", _shape("mechanical", {"role": {"enum": ["source", "gear", "actuator"]},
        "load_field": field_name, "speed_field": field_name,
        **{name: _integer(*bounds) for name, bounds in p.MECHANICAL_RANGES.items()}}))
    put("recorder", _shape("recorder", {name: _integer(*bounds) for name, bounds in p.RECORDER_RANGES.items()}))
    put("contract", _shape("contract", {"material": material_name,
        **{name: _integer(*bounds) for name, bounds in p.CONTRACT_RANGES.items()},
        "completion_when": _ref("condition"), "delivery_when": _ref("condition")}))
    components = {}
    for name, component in (component_catalog or {}).items():
        properties = {}
        for field, (kind, minimum, maximum) in component.fields.items():
            if field == "supports":
                continue  # Actual formed lots derive support; the compiler forbids authoring it.
            schema = {"type": {int: "integer", bool: "boolean", str: "string", dict: "object",
                                list: "array", float: "number"}[kind]}
            if minimum is not None:
                schema["minimum"] = minimum
            if maximum is not None:
                schema["maximum"] = maximum
            properties[field] = schema
        components[name] = _object(properties)
    put("components", _object(components))

    color = {"type": "string", "pattern": _full_pattern(display._COLOR)}
    glyph = _text(3)
    scalar = {"anyOf": [boolean, _integer()]}
    bindings = {}
    for target, rendered in (("color", color), ("glyph", glyph), ("pose", {"enum": list(display.POSES)})):
        bindings[target] = _object({"field": field_name,
            "cases": _array(_object({"value": scalar, "display": rendered}, ("value", "display")),
                display.MAX_CASES, minimum=1)}, ("field", "cases"))
    bindings["badge"] = _object({"field": field_name, "label": _text(display.MAX_BADGE_LABEL)}, ("field",))
    put("presentation", _object({"shape": {"enum": list(display.SHAPES)}, "color": color,
        "glyph": glyph, "bindings": _object(bindings)}))
    properties = {"name": _text(), "body_material": material_name,
        "cost": _object({name: _integer(0, p.PROGRAM_LIMITS["piece_cost"]) for name in body_materials}),
        "cost_u": _dictionary(_integer(1, p.PROGRAM_LIMITS["formation_u"]), material_name,
            maximum=p.PROGRAM_LIMITS["formation_materials"], minimum=1),
        "components": _ref("components"), "ports": _ref("ports"), "fields": _ref("fields"),
        "exports": _array(field_name, p.PROGRAM_LIMITS["exports"], unique=True),
        "container_capacity": _integer(1, p.PROGRAM_LIMITS["container_capacity"]),
        "mechanical": _ref("mechanical"), "recorder": _ref("recorder"), "contract": _ref("contract"),
        "barrier": _ref("condition"), "rules": _array(_ref("rule"), p.PROGRAM_LIMITS["rules"]),
        "actions": _array(_ref("action"), p.PROGRAM_LIMITS["actions"]), "presentation": _ref("presentation")}
    if set(properties) != set(p.TYPE_KEYS):
        raise ValueError("Drafting schema does not cover compiler type keys")
    put("type", _object(properties, p.TYPE_REQUIRED_KEYS, oneOf=[
        {"required": ["cost"], "not": {"required": ["cost_u"]}},
        {"required": ["cost_u"], "not": {"required": ["cost"]}}]))
    put("types", _dictionary(_ref("type"), symbol, maximum=p.PROGRAM_LIMITS["types"], minimum=1))
    put("contact_reaction", _shape("contact_reaction", {"id": key, "when": _ref("contact_condition"),
        "reaction": {"allOf": [_ref("reaction"), {"properties": {"heat": {"const": 0}}}]},
        "activation": {"enum": ["material", "installed"]}}))
    properties = {"key": key, "name": _text(), "version": _integer(1), "materials": _ref("materials"),
        "material_imports": _ref("material_imports"), "types": _ref("types"),
        "contact_reactions": _array(_ref("contact_reaction"), p.PROGRAM_LIMITS["contact_reactions"])}
    if set(properties) != set(p.PROGRAM_KEYS):
        raise ValueError("Drafting schema does not cover compiler definition keys")
    put("definition", _object(properties, p.PROGRAM_REQUIRED_KEYS))
    return defs


def _reachable(defs, root):
    needed = {}

    def visit(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                prefix = "#/$defs/"
                if not isinstance(reference, str) or not reference.startswith(prefix):
                    raise ValueError("Drafting grammar must use local references")
                name = reference[len(prefix):]
                if name not in needed:
                    if name not in defs:
                        raise ValueError("Dangling drafting schema reference: " + name)
                    needed[name] = defs[name]
                    visit(defs[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(root)
    return needed


def program_schema(*, materials, body_materials, max_custom_materials, component_catalog=None,
                   fact_catalog=None, node="definition"):
    """Return a self-contained draft schema for a supported node, not a draft.

    Limits that require counting a whole tree or comparing separate declarations
    remain compiler checks and are exposed as structured constraint metadata.
    """
    if node not in PROGRAM_SCHEMA_NODES:
        raise ValueError("Unknown program schema node")
    definitions = _definition_graph(materials=materials, body_materials=body_materials,
        max_custom_materials=max_custom_materials, component_catalog=component_catalog, fact_catalog=fact_catalog)
    result = {"$schema": "https://json-schema.org/draft/2020-12/schema", **_ref(node)}
    result["$defs"] = _reachable(definitions, result)
    result["x-program-compiler"] = {"schema_version": PROGRAM_SCHEMA_VERSION, "node": node,
        "final_authority": "compile_program", "canonical_definition_changed": False,
        "constraints": {"expression_max_depth": p.MAX_EXPRESSION_DEPTH,
            "expression_max_total_nodes": p.MAX_EXPRESSION_NODES,
            "local_and_imported_materials_combined": max_custom_materials,
            "literal_objects_and_ports_combined": p.PROGRAM_LIMITS["sources"],
            "formation_total_u": p.PROGRAM_LIMITS["formation_u"],
            "semantic_checks": ["declared_reference_and_result_types", "field_default_and_assignment_ranges",
                "immutable_import_and_export_resolution", "material_flags_and_feedstock_load",
                "reaction_mass_balance", "body_material_recipe_membership", "unique_declaration_ids",
                "drive_requires_source", "contact_material_activation", "printable_display_text",
                "strict_integer_input_types"]}}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PROGRAM_SCHEMA_BYTES:
        raise ValueError("Drafting grammar exceeds its byte budget; use smaller registry vocabularies")
    return result


def embed_program_schema(input_schema, *, property_name="definition", node="definition", **compiler_context):
    """Install grammar at an envelope's root so its local references resolve.

    This helper is for a drafting consumer. World validate/publish admission can
    deliberately keep accepting arbitrary draft objects for useful diagnostics.
    """
    grammar = program_schema(node=node, **compiler_context)
    result = deepcopy(input_schema)
    properties = result.setdefault("properties", {})
    if not isinstance(properties, dict) or not isinstance(property_name, str) or not property_name:
        raise ValueError("Drafting envelope requires object properties")
    definitions = result.setdefault("$defs", {})
    for name, value in grammar["$defs"].items():
        if name in definitions and definitions[name] != value:
            raise ValueError("Drafting envelope schema definition collision: " + name)
        definitions[name] = deepcopy(value)
    properties[property_name] = {"$ref": grammar["$ref"]}
    result["x-program-compiler"] = deepcopy(grammar["x-program-compiler"])
    return result


def program_schema_node_for_path(path):
    """Map a compiler diagnostic location to the smallest useful grammar node."""
    if not isinstance(path, str) or not (path == "program" or path.startswith("program.")):
        return None
    if path.startswith("program.contact_reactions"):
        if path == "program.contact_reactions":
            return "definition"
        if ".reaction" in path:
            return "stoichiometry" if re.search(r"\.(inputs|outputs)(?:$|\.)", path) else "contact_reaction"
        # Keep the restricted contact grammar instead of suggesting self,
        # event or object references from an ordinary expression node.
        return "contact_reaction"
    if re.search(r"\.effects\[\d+\]\.enabled$", path):
        return "condition"
    if re.search(r"\.(when|barrier|completion_when|delivery_when)(?:\.|\[)", path) or re.search(r"\.effects\[\d+\]\.(value|enabled)(?:$|\.|\[)", path):
        return "expression"
    if re.search(r"\.(when|barrier|completion_when|delivery_when)$", path):
        return "condition"
    if path.startswith("program.material_imports"):
        return "material_imports" if path == "program.material_imports" else "material_import"
    if path.startswith("program.materials"):
        return "materials" if path == "program.materials" else "material"
    if path == "program.types":
        return "types"
    if path.startswith("program.types."):
        for name in ("presentation", "mechanical", "recorder", "contract", "components"):
            if re.search(r"\." + name + r"(?:$|\.)", path):
                return name
        if ".ports" in path:
            if ".source" in path:
                return "source"
            return "ports" if path.endswith(".ports") else "port"
        if ".fields" in path or ".parameters" in path:
            return "fields" if path.endswith((".fields", ".parameters")) else "field"
        if re.search(r"\.effects\[\d+\]", path):
            return "stoichiometry" if re.search(r"\.(inputs|outputs)(?:$|\.)", path) else "effect"
        if ".rules" in path:
            return "type" if path.endswith(".rules") else "rule"
        if ".actions" in path:
            return "type" if path.endswith(".actions") else "action"
        return "type"
    return "definition"
