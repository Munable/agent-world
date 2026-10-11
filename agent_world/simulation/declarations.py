"""Ordering and write diagnostics for bounded declaration phases.

Conditions read the caller's frozen phase input. Effects retain the established
sequential candidate semantics: later rule ids overwrite earlier field writes.
Event applications preserve journal order before applying the same id policy.
This is execution policy, not an additional authored language or stored state.
"""

EXECUTION_SEMANTICS = "grove.program.v1"

PHASE_POLICY = {
    "ordinary_order": "rule_id_ascending",
    "event_order": "journal_order_then_rule_id_ascending",
    "field_conflict": "sequential_last_write",
    "condition_view": "frozen_phase_input",
    "effect_self_view": "current_instance_candidate",
}


def has_event(value):
    if isinstance(value, dict):
        return set(value) == {"event"} or any(has_event(child) for child in value.values())
    return isinstance(value, list) and any(has_event(child) for child in value)


def phase_rules(spec, phase):
    return sorted((rule for rule in spec["rules"] if rule["phase"] == phase),
                  key=lambda rule: rule["id"])


def phase_applications(spec, phase, events):
    rules = phase_rules(spec, phase)
    for rule in rules:
        if not has_event(rule):
            yield rule, None
    for fact in events:
        for rule in rules:
            if has_event(rule):
                yield rule, fact


def declaration_semantics(definition, *, type_id=None):
    """Report potential competing writers without rejecting existing programs.

    Conditions can be mutually exclusive, so these are possible writes rather
    than claims that both actually executed. Rule ids expose the existing winner
    policy; event applications additionally follow their causal journal order.
    """
    types = definition["types"]
    selected = {type_id: types[type_id]} if type_id is not None else types
    conflicts = {}
    for key, spec in selected.items():
        entries = []
        for phase in ("reaction", "after"):
            writers = {}
            for rule in phase_rules(spec, phase):
                for effect in rule["effects"]:
                    if effect["op"] == "set":
                        ids = writers.setdefault(effect["field"], [])
                        if rule["id"] not in ids:
                            ids.append(rule["id"])
                    elif effect["op"] == "drive":
                        ids = writers.setdefault("mechanical.enabled", [])
                        if rule["id"] not in ids:
                            ids.append(rule["id"])
            entries.extend({"phase": phase, "field": field, "rule_ids": ids,
                            "policy": PHASE_POLICY["field_conflict"]}
                           for field, ids in sorted(writers.items()) if len(ids) > 1)
        if entries:
            conflicts[key] = entries
    return {"phase_policy": dict(PHASE_POLICY), "potential_field_conflicts": conflicts}
