"""Compile trusted construction data without branches on construction names."""
from copy import deepcopy


def compile_composition(registry, definitions, spec):
    if not isinstance(spec, dict) or set(spec)-{"material", "construction", "size", "lining"}:
        raise ValueError("组合参数无效。")
    material, key, size, lining = spec.get("material"), spec.get("construction"), spec.get("size", 1), spec.get("lining")
    definition = definitions.get(key)
    if not definition or material not in definition["variants"] or type(size) is not int or size not in definition["sizes"]:
        raise ValueError("该材料、构造或大小尚未注册。")
    if lining is not None and lining not in definition.get("lining", []):
        raise ValueError("该衬材尚未注册。")
    variant = deepcopy(definition["variants"][material])
    for path in definition.get("scales", []):
        current, fields = variant, path.split(".")
        for field in fields[:-1]:
            current = current.get(field, {})
        if fields[-1] in current:
            current[fields[-1]] *= size
    components = variant["components"]
    cost = {mat: amount*size for mat, amount in variant["material_cost"].items()}
    matter = [{"id": "body", "material": material, "quantity": variant["quantity"], "slot": "body", "role": "structural", "burning": False}]
    for component_key in ("absorbent", "buoyant"):
        if component_key in components:
            components[component_key]["supports"] = {"body": {"quantity": variant["quantity"], "capacity": components[component_key]["capacity"]}}
    if lining:
        extra = definition["lining_definition"][lining]
        matter.append({"id": "lining", "material": lining, "quantity": extra["quantity"], "slot": "body", "role": "lining", "burning": False})
        for component, parameters in extra.get("components", {}).items():
            if component == "absorbent":
                existing = components.setdefault(component, {"capacity": 0, "rate": parameters["rate"]})
                existing["capacity"] += parameters["capacity"]
                existing.setdefault("supports", {})["lining"] = {"quantity": extra["quantity"], "capacity": parameters["capacity"]}
            else:
                components[component] = deepcopy(parameters)
        for mat, amount in extra["material_cost"].items():
            cost[mat] = cost.get(mat, 0)+amount
    for component, value in components.items():
        registry.components[component].validate(value)
    caps = ["transport"]
    caps += [tag for component, tag in (("container", "container"), ("absorbent", "wettable"), ("buoyant", "floating"), ("combustion", "flammable")) if component in components]
    canonical = {"material": material, "construction": key, "size": size}
    if lining:
        canonical["lining"] = lining
    return {"name": registry.materials[material].name+definition["name"]+("（"+registry.materials[lining].name+"衬）" if lining else ""),
            "material": material, "capabilities": caps, "state": deepcopy(definition.get("state", {})),
            "matter": matter, "components": components, "composition": canonical,
            "material_cost": cost, "material_units": sum(cost.values())*2,
            "water_capacity": components.get("container", {}).get("liquid_capacity", 0)}
