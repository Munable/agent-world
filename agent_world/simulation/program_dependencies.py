"""Derived dependencies of compiled data, without world reads or admission.

Initial contexts are supplied by trusted instance construction code. They are
declaration examples, never evidence about an existing instance. Human labels
and prose are deliberately not inputs to capability inference.
"""
from __future__ import annotations

from .facts import PUBLIC_FACTS


def _references(value, path):
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in {
                "field", "param", "quantity", "concentration", "physical",
                "mechanical", "contract", "event", "object"}:
            yield path, value
            return
        for key, child in value.items():
            yield from _references(child, path + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _references(child, path + f"[{index}]")


def _positive_requirement(expression, reference):
    """Prove a positive value is necessary along supported positive polarity.

    Negation and arithmetic are deliberately not inverted or simplified. An
    OR requires every alternative to need the value; an AND needs only one.
    """
    if not isinstance(expression, dict):
        return False
    op, left, right = expression.get("op"), expression.get("left"), expression.get("right")
    if right == reference and type(left) is int:
        left, right = right, left
        op = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}.get(op, op)
    if left == reference and type(right) is int:
        if (op == "gt" and right >= 0 or op == "gte" and right > 0
                or op == "eq" and right > 0 or op == "ne" and right == 0):
            return True
    if op in {"all", "any"}:
        args = expression.get("args")
        if not isinstance(args, list) or not args:
            return False
        necessary = (_positive_requirement(child, reference) for child in args)
        return any(necessary) if op == "all" else all(necessary)
    return False


def _require(condition, *, known=False, actual=None, required=True, satisfied=None, **extra):
    if satisfied is None and known:
        satisfied = actual == required
    return {"condition": condition, "scope": "declared_initial" if known else "world",
            "known": known, "satisfied": satisfied,
            "actual": actual, "required": required, **extra}


def _initial(context, group, name):
    values = context.get(group, {})
    known = name in values and type(values[name]) in {bool, int, str}
    return {"scope": "declared_initial", "known": known,
            "value": values[name] if known else None}


def _material(definition, registry, symbol):
    if symbol in definition.get("materials", {}):
        return definition["materials"][symbol]
    current = getattr(registry, "materials", {}).get(symbol)
    return current.snapshot() if current is not None else None


def dependency_items(definition, *, registry=None, initial_contexts=None):
    """Yield facts and prerequisites; this is not a whole-program proof.

    A provided dependency is structurally declared, not necessarily true now.
    Unbound ports, actual material lots and future events remain deferred.
    ``initial_contexts`` uses the evaluator's context keys plus components and
    capacities supplied by the world's pure constructor.
    """
    initial_contexts = initial_contexts or {}

    def row(type_id, path, reference, kind, *, status="provided", code="dependency_declared",
            resolution="declared_state", topics=("program",), requirements=(), initial=None,
            strategy="inspect_actual_instance", paths=(), **extra):
        return {"code": code, "severity": "info", "type_id": type_id,
                "kind": kind, "path": path, "reference": reference,
                "status": status, "resolution": resolution,
                "requirements": list(requirements),
                "initial": initial or {"scope": "declared_initial", "known": False, "value": None},
                "evidence": {"basis": "compiled_declaration_and_trusted_semantics"},
                "remediation": {"strategy": strategy, "paths": list(paths)},
                "catalog_topics": list(topics), **extra}

    def read(type_id, spec, context, path, reference, expression):
        kind, name = next(iter(reference.items()))
        prefix = "program.types." + type_id
        if kind == "field":
            return row(type_id, path, reference, "self_field",
                initial={"scope": "declared_initial", "known": True,
                         "value": spec["fields"][name]["default"]},
                paths=(prefix + ".fields." + name,),
                requirements=(_require("declared_field", known=True, actual=True),))
        if kind == "param":
            return row(type_id, path, reference, "action_param", status="deferred",
                resolution="runtime_input", code="manual_input_required",
                requirements=(_require("actual_typed_action_parameter", parameter=name),),
                strategy="choose_declared_action_input", paths=(path,))
        if kind in {"quantity", "concentration"}:
            initial = _initial(context, "quantities", name)
            if "quantities" in context:
                amount = context["quantities"].get(name, 0)
                initial = {"scope": "declared_initial", "known": True,
                    "value": amount if kind == "quantity" else
                        amount * 100 // max(1, sum(context["quantities"].values()))}
            positive = _positive_requirement(expression, reference)
            return row(type_id, path, reference, "self_" + kind, status="deferred",
                code="actual_contained_material_required" if positive else "contained_material_fact",
                resolution="runtime_fact",
                initial=initial, topics=("materials",),
                requirements=(_require("actual_contained_material" if positive else "actual_contained_quantity",
                    material=name, required={"minimum": 1} if positive else None,
                    purpose="positive_branch" if positive else "read"),),
                strategy="supply_actual_material" if positive else "inspect_actual_instance",
                paths=(prefix + ".container_capacity",))
        if kind == "object":
            public_field = name["field"]
            port = name.get("port")
            declaration = spec.get("ports", {}).get(port, {})
            requirements = [_require("readable_external_fact", field=public_field)]
            if port is not None:
                requirements.insert(0, _require("actual_port_binding", port=port))
                if declaration.get("source"):
                    requirements.append(_require("pinned_source_interface", source=declaration["source"]))
            fact = PUBLIC_FACTS.get(public_field)
            if fact and fact.component:
                requirements.append(_require("external_component", component=fact.component))
            return row(type_id, path, reference, "port_fact" if port else "literal_object_fact",
                status="deferred", code="external_fact_requires_world_read",
                resolution="runtime_binding" if port else "runtime_fact",
                topics=("ports", "physical"), requirements=requirements,
                strategy="bind_and_inspect_declared_source" if port else "inspect_declared_source",
                paths=(prefix + ".ports." + port,) if port else (path,),
                source=declaration.get("source"),
                unavailable_behavior="fact_unavailable" if port or public_field.startswith("physical.")
                    else "evaluation_error")
        if kind == "event":
            return row(type_id, path, reference, "event_context", status="deferred",
                code="actual_event_required", resolution="runtime_event", topics=("events",),
                requirements=(_require("actual_matching_journal_event", field=name),),
                strategy="produce_and_observe_actual_event", paths=(path,))
        if kind in {"mechanical", "contract"}:
            present = kind in spec
            initial = _initial(context, kind, name)
            positive = _positive_requirement(expression, reference)
            missing = not present and positive
            return row(type_id, path, reference, "self_" + kind,
                status="missing" if missing else "provided" if initial["known"] or present else "unknown",
                code="cannot_change_without_mechanism" if not present else "mechanism_fact_declared",
                resolution="runtime_fact" if present else "default_constant" if initial["known"] else "unknown",
                initial=initial, topics=(kind,),
                requirements=(_require("declared_" + kind, known=True, actual=present,
                    purpose="read_support" if present else "change"),),
                strategy="revise_definition" if missing else "inspect_actual_instance",
                paths=(prefix + "." + kind,), severity="warning" if missing else "info")
        if kind == "physical":
            fact = PUBLIC_FACTS["physical." + name]
            initial = _initial(context, "physical", name)
            requirements = []
            status, code, resolution = "provided", "physical_fact_declared", "runtime_fact"
            topics = ("physical",)
            paths = ()
            if fact.component:
                complete = "components" in context
                present = fact.component in context.get("components", spec.get("components", {}))
                requirements.append(_require("effective_component", known=complete or present,
                    actual=present if complete or present else None, component=fact.component))
                status = "missing" if complete and not present else "provided" if present else "unknown"
                code = "self_fact_component_missing" if status == "missing" else code
                topics = ("components", "physical")
                paths = (prefix + ".components." + fact.component, "component_schema." + fact.component)
            if name in {"wetness", "absorbed_water"}:
                capacity = context.get("capacities", {}).get("absorbent")
                if capacity is None and "absorbent" in spec.get("components", {}):
                    capacity = spec["components"]["absorbent"].get("capacity")
                positive = _positive_requirement(expression, reference)
                requirements.extend((
                    _require("effective_absorbent_capacity", known=capacity is not None,
                        actual=capacity, required={"minimum": 1}, satisfied=capacity > 0 if capacity is not None else None,
                        purpose="change"),
                    _require("actual_absorbed_water", required={"minimum": 1}, purpose="change")))
                if capacity == 0:
                    code = "cannot_change_without_support"
                    status = "missing" if positive else "provided"
                    resolution = "default_constant" if name == "wetness" else "runtime_fact"
                topics = ("components", "physical")
                paths = (prefix + ".components.absorbent.capacity", "component_schema.absorbent")
            elif name == "burning":
                # Burning is a lot property. combustion adjusts its rate, and
                # is not required for a real combustible lot to be burning.
                requirements.extend((_require("actual_combustible_lot", purpose="change"),
                                     _require("actual_ignition", purpose="change")))
                topics = ("materials", "physical")
            elif name in {"surface_water", "contained_water"}:
                requirements.append(_require("actual_" + name, required={"minimum": 1}, purpose="change"))
                topics = ("materials", "physical")
            if fact.aggregate:
                requirements.append(_require("actual_visible_relationship_snapshot"))
                status = "deferred" if status != "missing" else status
                # Attached descendants may provide buoyancy: absence of a
                # local buoyant component never proves a permanent zero.
            return row(type_id, path, reference, "self_physical", status=status, code=code,
                resolution=resolution, initial=initial, topics=topics, requirements=requirements,
                strategy="revise_definition" if status == "missing" else "inspect_actual_instance",
                paths=paths, severity="warning" if status == "missing" else "info")

    for type_id, spec in definition["types"].items():
        context = initial_contexts.get(type_id, {})
        prefix = "program.types." + type_id
        # Construction consumes actual matter; declarations and material
        # imports never establish that the participant possesses those lots.
        costs = spec.get("cost_u", spec.get("cost", {}))
        yield row(type_id, prefix + (".cost_u" if "cost_u" in spec else ".cost"), costs,
            "formation_material", status="deferred", code="actual_formation_material_required",
            resolution="runtime_input", topics=("materials",),
            requirements=tuple(_require("actual_formation_material", material=symbol,
                required={"minimum": amount}, unit="u" if "cost_u" in spec else "piece",
                material_semantics={key: value for key, value in (_material(definition, registry, symbol) or {}).items()
                    if key in {"load_per_u", "combustible", "liquid"}})
                for symbol, amount in costs.items() if amount), strategy="obtain_actual_recipe_inputs",
            paths=(prefix + ".cost_u" if "cost_u" in spec else prefix + ".cost",))
        for port, declaration in spec.get("ports", {}).items():
            yield row(type_id, prefix + ".ports." + port, {"port": port, **declaration},
                "declared_port", status="deferred", code="port_binding_required",
                resolution="runtime_binding", topics=("ports",),
                requirements=(_require("actual_port_binding", port=port),),
                strategy="bind_and_inspect_declared_source", paths=(prefix + ".ports." + port,),
                source=declaration.get("source"))
        for mechanism in ("mechanical", "contract", "recorder"):
            if mechanism not in spec:
                continue
            requirements = [_require("declared_" + mechanism, known=True, actual=True)]
            if mechanism == "mechanical":
                source = spec[mechanism]["role"] == "source"
                requirements.append(_require("enabled_source_and_actual_energy" if source else "connected_active_source"))
            elif mechanism == "contract":
                requirements.extend((_require("actual_party_acceptance"), _require("actual_delivery_and_completion")))
            else:
                requirements.append(_require("actual_recording_session_and_events"))
            yield row(type_id, prefix + "." + mechanism, spec[mechanism], "mechanism",
                status="deferred", code="mechanism_requires_actual_inputs", resolution="runtime_input",
                topics=("events" if mechanism == "recorder" else mechanism,), requirements=requirements,
                strategy="inspect_and_supply_mechanism_inputs", paths=(prefix + "." + mechanism,))
        expressions = []
        for group in ("rules", "actions"):
            for index, declaration in enumerate(spec[group]):
                path = prefix + f".{group}[{index}]"
                location = {"phase": declaration.get("phase", "manual"),
                            "rule_id" if group == "rules" else "action_id": declaration["id"]}
                expressions.append((declaration.get("when", True), path + ".when", location))
                for index, effect in enumerate(declaration["effects"]):
                    ep = path + f".effects[{index}]"
                    if effect["op"] in {"set", "drive"}:
                        key = "value" if effect["op"] == "set" else "enabled"
                        expressions.append((effect[key], ep + "." + key, location))
                    elif effect["op"] == "react":
                        complete = "components" in context
                        container = "container" in context.get("components", {})
                        missing = complete and not container
                        yield row(type_id, ep, effect["inputs"], "reaction_input", status="missing" if missing else "deferred",
                            code="reaction_container_missing" if missing else "actual_reaction_inputs_required",
                            resolution="runtime_input", topics=("materials", "components"),
                            requirements=tuple(_require("actual_nonburning_unreserved_contained_material",
                                material=symbol, required={"minimum": amount}) for symbol, amount in effect["inputs"].items()) +
                                (_require("effective_reaction_container", known=complete,
                                    actual=container if complete else None), _require("actual_reaction_capacity")),
                            strategy="revise_definition" if missing else "supply_actual_reaction_inputs",
                            paths=(prefix + ".container_capacity", "component_schema.container"),
                            severity="warning" if missing else "info", **location)
        if "barrier" in spec:
            expressions.append((spec["barrier"], prefix + ".barrier", {"phase": "after"}))
        for condition in ("completion_when", "delivery_when"):
            if condition in spec.get("contract", {}):
                expressions.append((spec["contract"][condition], prefix + ".contract." + condition, {"phase": "contract"}))
        for expression, path, location in expressions:
            for at, reference in _references(expression, path):
                item = read(type_id, spec, context, at, reference, expression)
                if item is not None:
                    yield {**item, **location}
    for index, declaration in enumerate(definition.get("contact_reactions", [])):
        path = f"program.contact_reactions[{index}]"
        yield row(None, path, declaration["reaction"]["inputs"], "contact_reaction_input",
            status="deferred", code="actual_contact_reaction_inputs_required", resolution="runtime_input",
            topics=("materials",), requirements=(
                _require("actual_contact_materials_and_conditions"),
                _require("actual_local_installation" if declaration.get("activation", "material") == "installed" else "actual_material_activation")),
            strategy="supply_and_observe_actual_contact_inputs", paths=(path,), rule_id=declaration["id"])
    for alias, source in definition.get("material_imports", {}).items():
        path = "program.material_imports." + alias
        yield row(None, path, {"alias": alias, **source}, "material_import",
            status="deferred", code="import_requires_actual_material_lot", resolution="runtime_input",
            topics=("materials",), requirements=(
                _require("immutable_material_binding", known=True,
                    actual=source["definition_ref"] + "#" + source["symbol"],
                    required=source["definition_ref"] + "#" + source["symbol"]),
                _require("actual_original_material_lot")),
            strategy="obtain_original_material_identity", paths=(path,), source=source)
