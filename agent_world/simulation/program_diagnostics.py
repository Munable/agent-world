"""Bounded, derived advice for compiled programs; never execution permission.

Intervals cover local integer expressions and simple guards. They deliberately
do not prove reachability, termination, external facts or whole-world safety.
Nothing in this module edits a definition or changes its interpreter semantics.
"""
from __future__ import annotations

import json
from .declarations import has_event
from .program_dependencies import dependency_items

MAX_DIAGNOSTICS = 16
MAX_DIAGNOSTIC_BYTES = 8192
MAX_RUNTIME_DIAGNOSTIC_BYTES = 512


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _domains(specs):
    return {name: (spec["min"], spec["max"]) for name, spec in specs.items()
            if spec["type"] == "integer"}


def _guard(expr, domains):
    """Narrow only a conjunction of declared-field/literal integer comparisons."""
    result = dict(domains)
    if expr is False:
        return None
    if not isinstance(expr, dict):
        return result
    if expr.get("op") == "all":
        for item in expr["args"]:
            result = _guard(item, result)
            if result is None:
                return None
        return result
    left, right, op = expr.get("left"), expr.get("right"), expr.get("op")
    if type(left) is int and isinstance(right, dict) and set(right) == {"field"}:
        left, right = right, left
        op = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}.get(op, op)
    if not (isinstance(left, dict) and set(left) == {"field"}
            and left["field"] in result and type(right) is int):
        return result
    lo, hi = result[left["field"]]
    if op == "eq":
        lo, hi = max(lo, right), min(hi, right)
    elif op == "lt":
        hi = min(hi, right-1)
    elif op == "lte":
        hi = min(hi, right)
    elif op == "gt":
        lo = max(lo, right+1)
    elif op == "gte":
        lo = max(lo, right)
    elif op == "ne":
        if lo == right:
            lo += 1
        if hi == right:
            hi -= 1
    if lo > hi:
        return None
    result[left["field"]] = lo, hi
    return result


def _interval(expr, fields, params, issues, path):
    if type(expr) is int:
        return expr, expr
    if not isinstance(expr, dict):
        return None
    if set(expr) == {"field"}:
        return fields.get(expr["field"])
    if set(expr) == {"param"}:
        return params.get(expr["param"])
    op = expr.get("op")
    if "left" not in expr or "right" not in expr:
        # Still examine nested arithmetic for a zero denominator warning.
        for key in ("value", "args"):
            children = expr.get(key, [])
            children = children if isinstance(children, list) else [children]
            for index, child in enumerate(children):
                _interval(child, fields, params, issues, path+"."+key+f"[{index}]")
        return None
    left = _interval(expr["left"], fields, params, issues, path+".left")
    right = _interval(expr["right"], fields, params, issues, path+".right")
    if op == "div" and (right is None or right[0] <= 0 <= right[1]):
        issues.append({"code": "division_by_zero_possible", "path": path,
                       "message": "除数的范围可能包含0；未知输入不等于安全。"})
    if left is None or right is None:
        return None
    a, b = left
    c, d = right
    if op == "add":
        return a+c, b+d
    if op == "sub":
        return a-d, b-c
    if op == "mul":
        values = (a*c, a*d, b*c, b*d)
        return min(values), max(values)
    if op == "div" and not c <= 0 <= d:
        values = (a//c, a//d, b//c, b//d)
        return min(values), max(values)
    if op == "min":
        return min(a, c), min(b, d)
    if op == "max":
        return max(a, c), max(b, d)
    return None


def _written_fields(effect):
    if effect["op"] == "set":
        return {effect["field"]}
    if effect["op"] == "react" and effect["heat"]:
        return {"temperature"}
    return set()


def _advance_field(field, interval, declared, fields, issues, path):
    if field not in declared:
        return
    if interval is None:
        fields[field] = declared[field]
        return
    lo, hi = interval
    minimum, maximum = declared[field]
    if lo < minimum or hi > maximum:
        issues.append({"code": "field_range_possible", "path": path, "field": field,
            "inferred_min": lo, "inferred_max": hi, "min": minimum, "max": maximum,
            "message": "此赋值在声明范围内的某些输入上可能越界；条件或表达式需要检查。"})
    # Only successful candidates reach a following effect. A wholly invalid
    # interval stays conservative rather than certifying an unreachable branch.
    valid_lo, valid_hi = max(lo, minimum), min(hi, maximum)
    fields[field] = (valid_lo, valid_hi) if valid_lo <= valid_hi else declared[field]


def program_diagnostics(definition, *, registry=None, initial_contexts=None, read_requests=None):
    """Advise on a canonical compiled definition with explicit bounded scope."""
    items, omitted = [], 0

    def append(item):
        nonlocal omitted
        item = {"severity": "warning", **item}
        if len(items) >= MAX_DIAGNOSTICS or _bytes(items+[item]) > MAX_DIAGNOSTIC_BYTES-1024:
            omitted += 1
        else:
            items.append(item)

    for type_id, spec in definition["types"].items():
        declared = _domains(spec["fields"])
        # A previous candidate write invalidates narrowing by the frozen guard.
        # Starting each declaration from the full declared domain is conservative.
        for group in ("rules", "actions"):
            for index, rule in enumerate(spec[group]):
                phase = rule.get("phase", "manual")
                prefix = f"program.types.{type_id}.{group}[{index}]"
                location = {"type_id": type_id, "phase": phase,
                            "rule_id" if group == "rules" else "action_id": rule["id"]}
                condition = rule.get("when", True)
                narrowed = _guard(condition, declared)
                if narrowed is None:
                    continue
                fields = dict(narrowed)
                # Execution orders by id/events rather than declaration order.
                # Any other writer (or repeated event application) may already
                # have changed the candidate before this frozen guard is used.
                prior_writes = {field for other in spec["rules"]
                                if group == "rules" and other["phase"] == phase
                                and (other["id"] != rule["id"] or has_event(rule))
                                and _guard(other.get("when", True), narrowed) is not None
                                for effect in other["effects"] for field in _written_fields(effect)}
                for name in prior_writes:
                    if name in declared:
                        fields[name] = declared[name]
                params = _domains(rule.get("parameters", {}))
                issues = []
                _interval(condition, declared, params, issues, prefix+".when")
                for effect_index, effect in enumerate(rule["effects"]):
                    path = prefix+f".effects[{effect_index}]"
                    if effect["op"] == "set":
                        interval = _interval(effect["value"], fields, params, issues, path+".value")
                        _advance_field(effect["field"], interval, declared, fields, issues, path+".value")
                    elif effect["op"] == "react" and effect["heat"] and "temperature" in declared:
                        # The real interpreter adds heat*extent to temperature.
                        # Extent can be0..rate; available matter is outside this
                        # local analysis, so do not infer a reaction occurred.
                        lo, hi = fields["temperature"]
                        maximum_heat = effect["heat"] * effect["rate"]
                        interval = lo+min(0, maximum_heat), hi+max(0, maximum_heat)
                        _advance_field("temperature", interval, declared, fields, issues, path+".heat")
                    elif effect["op"] == "drive":
                        _interval(effect["enabled"], fields, params, issues, path+".enabled")
                for issue in issues:
                    append({**location, **issue})
    result = {"items": items, "truncated": omitted > 0, "omitted_count": omitted,
            "safety_proven": False,
            "analysis_scope": "Local integer intervals and simple field guards only; warnings are not execution permission. "
                "No reachability, termination, external-fact, resource, multi-object or complete multi-rule safety proof. "
                "Frozen predicates and prior candidate writes are conservatively distinguished."}
    dependencies = {"items": [], "truncated": False, "omitted_count": 0,
        "static_only": True, "world_facts_checked": False, "execution_proven": False,
        "analysis_scope": "Typed references, declared mechanisms and trusted construction initial contexts. "
            "Provided means declared support, not a satisfied live condition. Deferred inputs need world evidence. "
            "No branch reachability, material availability, visibility, event occurrence or outcome proof."}
    result["dependency_analysis"] = dependencies
    # Keep actionable missing capabilities before the ordinary declaration
    # inventory, without sacrificing the existing interval warnings.
    candidates = list(dependency_items(definition, registry=registry, initial_contexts=initial_contexts))
    candidates.sort(key=lambda item: item["status"] != "missing")
    for item in candidates:
        if read_requests is not None:
            item["read_requests"] = read_requests(item)
        if len(dependencies["items"]) >= 24 or _bytes(result) + _bytes(item) > MAX_DIAGNOSTIC_BYTES - 128:
            dependencies["omitted_count"] += 1
        else:
            dependencies["items"].append(item)
    dependencies["truncated"] = dependencies["omitted_count"] > 0
    return result


def invalid_diagnostics(error):
    message = str(error)[:500]
    path, separator, explanation = message.partition(":")
    return {"items": [{"severity": "error", "code": "invalid_definition",
                       "path": path[:180] if separator else "program",
                       "message": explanation.strip() if separator else message}],
            "truncated": False, "omitted_count": 0, "safety_proven": False,
            "analysis_scope": "The current formal compiler rejected this definition; nothing was published."}


class ProgramEvaluationError(ValueError):
    """A real failed evaluation with bounded location, never a reconstructed run."""
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def runtime_diagnostic(error, *, logical_step):
    detail = getattr(error, "diagnostic", None)
    if not isinstance(detail, dict):
        return None
    result = {**detail, "logical_step": logical_step}
    if _bytes(result) <= MAX_RUNTIME_DIAGNOSTIC_BYTES:
        return result
    # Preserve the actual compact location, never invent a missing value.
    result = {key: result[key] for key in ("code", "rule_id", "action_id", "phase", "effect_index", "logical_step")
              if key in result}
    result["detail_omitted"] = "diagnostic_byte_limit"
    return result if _bytes(result) <= MAX_RUNTIME_DIAGNOSTIC_BYTES else None
