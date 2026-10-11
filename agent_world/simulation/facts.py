"""Finite, typed facts read from supplied physical snapshots; no world access."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from . import matter as sm


class FactUnavailable(ValueError):
    """An applicable, declared fact cannot currently be established."""


@dataclass(frozen=True)
class FactSpec:
    type: str
    component: str | None = None
    aggregate: bool = False
    unit: str | None = None
    meaning: str | None = None


PUBLIC_FACTS = {
    "intact": FactSpec("boolean"),
    "barrier.active": FactSpec("boolean", "barrier"),
    "barrier.opened_sequence": FactSpec("integer", "barrier"),
    **{"mechanical." + key: FactSpec("string" if key == "status" else "integer", "mechanical")
       for key in ("position", "speed", "angle", "energy", "status", "available_torque")},
    "physical.wetness": FactSpec("integer", unit="%", meaning="吸水饱和百分值：已吸附水量除以有效吸水容量，取整数且至多100%；容量为0时为0。不是水量u，不计表面或容纳槽的水。"),
    "physical.burning": FactSpec("boolean"),
    "physical.surface_water": FactSpec("integer", unit="u", meaning="实际表面槽的水量。"),
    "physical.contained_water": FactSpec("integer", unit="u", meaning="实际容纳槽的水量。"),
    "physical.absorbed_water": FactSpec("integer", unit="u", meaning="实际吸附槽的水量。"),
    "physical.structure.integrity": FactSpec("integer", "structure"),
    "physical.structure.max_integrity": FactSpec("integer", "structure"),
    **{"physical.load." + key: FactSpec("integer", "structure" if key in {"limit", "overload"} else None,
                                         key != "local")
       for key in ("local", "total", "supported", "limit", "overload")},
    "physical.buoyancy": FactSpec("integer", aggregate=True),
    "physical.can_float": FactSpec("boolean", aggregate=True),
}
FACT_TYPES = {key: spec.type for key, spec in PUBLIC_FACTS.items()}
PHYSICAL_TYPES = {key.removeprefix("physical."): spec.type for key, spec in PUBLIC_FACTS.items()
                  if key.startswith("physical.")}


def fact_catalog():
    return {name: asdict(spec) for name, spec in PUBLIC_FACTS.items()}


def reference_semantics(reference):
    """Describe a reference without inferring units from an authored field name."""
    if not isinstance(reference, dict):
        return None
    name = ("physical." + reference["physical"] if "physical" in reference else
            "mechanical." + reference["mechanical"] if "mechanical" in reference else
            reference.get("object", {}).get("field"))
    spec = PUBLIC_FACTS.get(name)
    if spec is not None and spec.unit is not None:
        return {"unit": spec.unit}
    if "quantity" in reference:
        return {"unit": "u"}
    if "concentration" in reference:
        return {"unit": "%"}
    return None


def snapshot_facts(registry, entity, objects=None, *, aggregate_known=True):
    """Omit inapplicable/hidden facts rather than inventing display defaults."""
    facts = {"intact": entity.get("lifecycle", "intact") == "intact"}
    components = entity.get("components", {})
    binding = entity.get("state", {}).get("program", {})
    if "barrier" in components:
        facts.update({"barrier.active": components["barrier"]["active"],
                      "barrier.opened_sequence": binding.get("barrier_opened_sequence", 0)})
    if "mechanical" in binding:
        for key in ("position", "speed", "angle", "energy", "status", "available_torque"):
            if key in binding["mechanical"]:
                value = binding["mechanical"][key]
                facts["mechanical." + key] = value if key == "status" else int(value)
    facts.update({"physical.wetness": sm.moisture(entity),
                  "physical.burning": any(lot["quantity"] > 0 and lot.get("burning") for lot in entity.get("matter", [])),
                  **{"physical." + slot + "_water": sm.quantity(entity, "water", slot)
                     for slot in ("surface", "contained", "absorbed")}})
    for key in ("integrity", "max_integrity"):
        if key in components.get("structure", {}):
            facts["physical.structure." + key] = components["structure"][key]
    summary = sm.load_summary(registry, entity, objects or {entity.get("id"): entity})
    for key in ("local", "total", "supported", "limit", "overload"):
        name = "physical.load." + key
        if (aggregate_known or not PUBLIC_FACTS[name].aggregate) and (PUBLIC_FACTS[name].component is None or "structure" in components):
            facts[name] = summary[key]
    if aggregate_known:
        facts["physical.buoyancy"] = summary["buoyancy"]
        facts["physical.can_float"] = summary["can_float"]
    return facts


def reference_value(values, name):
    if name not in values:
        raise FactUnavailable("事实当前不可用：" + name)
    return values[name]
