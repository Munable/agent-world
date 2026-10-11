"""Pure dependency plans for the existing bounded declaration interpreter.

Plans describe possible reads and writes, never predict a condition's result.
They are derived metadata; immutable program data and its hash stay untouched.
"""
from copy import deepcopy

from .declarations import EXECUTION_SEMANTICS
from .programs import exported_interface


_READ_GROUPS = {
    "field": "fields", "physical": "physical", "mechanical": "mechanical",
    "contract": "contract", "quantity": "quantities",
    "concentration": "concentrations", "event": "events",
}


def _reads(value, groups, objects, ports):
    if isinstance(value, dict):
        if len(value) == 1:
            key, reference = next(iter(value.items()))
            if key in _READ_GROUPS:
                if not isinstance(reference, str):
                    raise ValueError("Execution dependencies require named references")
                groups[_READ_GROUPS[key]].add(reference)
                return
            if key == "object":
                if (not isinstance(reference, dict) or set(reference) - {"id", "port", "field"}
                        or ("id" in reference) == ("port" in reference)
                        or not isinstance(reference.get("field"), str)):
                    raise ValueError("Execution dependencies require one typed object binding")
                name = reference.get("id", reference.get("port"))
                if not isinstance(name, str):
                    raise ValueError("Execution dependencies require a named object or port")
                target = objects if "id" in reference else ports
                target.setdefault(name, set()).add(reference["field"])
                return
        for child in value.values():
            _reads(child, groups, objects, ports)
    elif isinstance(value, list):
        for child in value:
            _reads(child, groups, objects, ports)


def _type_plan(spec):
    if (not isinstance(spec, dict) or not isinstance(spec.get("fields"), dict)
            or not isinstance(spec.get("rules"), list)):
        raise ValueError("Execution plans require canonical fields and rules")
    groups = {key: set() for key in _READ_GROUPS.values()}
    objects, ports, writes, effects, rule_fields = {}, {}, set(), set(), set()
    material_writes = set()
    drive = False
    for rule in spec["rules"]:
        local = {key: set() for key in _READ_GROUPS.values()}
        _reads(rule["when"], local, objects, ports)
        for effect in rule["effects"]:
            op = effect["op"]
            if op not in {"set", "drive", "react", "emit"}:
                raise ValueError("Execution plan encountered an unsupported declaration effect")
            effects.add(op)
            if op == "set":
                writes.add(effect["field"])
                _reads(effect["value"], local, objects, ports)
            elif op == "drive":
                drive = True
                _reads(effect["enabled"], local, objects, ports)
            elif op == "react":
                local["quantities"].update(effect["inputs"])
                material_writes.update(effect["inputs"])
                material_writes.update(effect["outputs"])
                if effect.get("heat") and "temperature" in spec["fields"]:
                    local["fields"].add("temperature")
                    writes.add("temperature")
        rule_fields.update(local["fields"])
        for key, names in local.items():
            groups[key].update(names)
    if "barrier" in spec:
        _reads(spec["barrier"], groups, objects, ports)
    contract = spec.get("contract")
    if contract is not None:
        for key in ("completion_when", "delivery_when"):
            if key in contract:
                _reads(contract[key], groups, objects, ports)
    declared_ports = spec.get("ports", {})
    if set(ports) - set(declared_ports):
        raise ValueError("Execution plan references an undeclared port")
    if writes - set(spec["fields"]):
        raise ValueError("Execution plan writes an undeclared self field")
    feedback = bool(rule_fields & writes)
    mechanical, recorder = "mechanical" in spec, "recorder" in spec
    if mechanical:
        groups["fields"].update(spec["mechanical"][name] for name in ("load_field", "speed_field")
                                if name in spec["mechanical"])
    physical = bool(groups["physical"])
    blockers = []
    for enabled, reason in (
        ("react" in effects, "reaction"), ("emit" in effects, "emit"),
        (feedback, "self_feedback"), (mechanical, "mechanical"),
        (contract is not None, "contract"), (recorder, "recorder"),
        (physical, "self_physical_input"), (bool(groups["mechanical"]), "self_mechanical_input"),
        (bool(groups["contract"]), "self_contract_input"),
    ):
        if enabled:
            blockers.append(reason)
    return {
        "reads": {**{key: sorted(names) for key, names in groups.items()},
                  "objects": {key: sorted(names) for key, names in sorted(objects.items())},
                  "ports": {key: sorted(names) for key, names in sorted(ports.items())}},
        "writes": {"fields": sorted(writes), "mechanical": ["enabled"] if drive else [],
                   "quantities": sorted(material_writes)},
        "effects": sorted(effects), "declared_ports": deepcopy(declared_ports),
        "literal_objects": sorted(objects), "exports": exported_interface(spec),
        "sleep_safe": not blockers, "sleep_blockers": blockers,
        "continuous": bool(blockers), "self_feedback": feedback,
        "event_driven": bool(groups["events"]), "mechanical": mechanical,
        "contract": contract is not None, "recorder": recorder, "physical": physical,
        "reaction": "react" in effects, "emit": "emit" in effects,
    }


def execution_plan(definition, *, type_id=None):
    """Analyze canonical types; actions wake their instance at the world boundary.

    Non-feedback field rules can sleep after settlement until a recorded input
    changes. Events need explicit journal wakeups. Stateful solvers, reactions,
    self feedback and emit remain conservatively active; a world may refine this
    using actual activity, but cannot infer missing input to be zero or false.
    """
    if not isinstance(definition, dict) or not isinstance(definition.get("types"), dict):
        raise ValueError("Execution plans require a canonical program definition")
    types = definition["types"]
    if type_id is not None:
        if type_id not in types:
            raise ValueError("Execution plan type does not exist in this definition")
        types = {type_id: types[type_id]}
    return {"semantics": EXECUTION_SEMANTICS,
            "types": {key: _type_plan(spec) for key, spec in types.items()}}
