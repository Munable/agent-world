"""Trusted world-owned definitions; this is not a plugin sandbox or a DSL."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class MaterialDef:
    id: str
    name: str
    load_per_u: int
    piece_quantity: int
    combustible: bool = False
    absorption_per_piece: int = 0
    liquid: bool = False


@dataclass(frozen=True)
class ComponentDef:
    id: str
    fields: dict[str, tuple[type, int | None, int | None]]

    def validate(self, value):
        if not isinstance(value, dict) or set(value) - set(self.fields):
            raise ValueError(f"Invalid {self.id} component")
        for key, current in value.items():
            kind, minimum, maximum = self.fields[key]
            if type(current) is not kind:
                raise ValueError(f"Invalid {self.id}.{key}")
            if minimum is not None and current < minimum:
                raise ValueError(f"Invalid {self.id}.{key}")
            if maximum is not None and current > maximum:
                raise ValueError(f"Invalid {self.id}.{key}")


@dataclass(frozen=True)
class RuleSpec:
    id: str
    version: int
    phase: str
    evaluate: Callable
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reducers: dict[str, str] = field(default_factory=dict)
    after: tuple[str, ...] = ()
    sources: frozenset[str] = frozenset()
    sinks: frozenset[str] = frozenset()
    max_participants: int = 64
    max_effects: int = 1024


class Registry:
    def __init__(self, version, phases):
        self.version = version
        self.phases = tuple(phases)
        self.materials = {}
        self.components = {}
        self.rules = {}

    def register_material(self, definition):
        if definition.id in self.materials:
            raise ValueError("Duplicate material")
        self.materials[definition.id] = definition
        return definition

    def register_component(self, definition):
        if definition.id in self.components:
            raise ValueError("Duplicate component")
        self.components[definition.id] = definition
        return definition

    def register_rule(self, definition):
        if definition.id in self.rules or definition.phase not in self.phases:
            raise ValueError("Duplicate rule or unknown phase")
        if definition.version < 1 or not 1 <= definition.max_participants <= 64 or not 1 <= definition.max_effects <= 1024:
            raise ValueError("Rule must declare finite limits")
        self.rules[definition.id] = definition
        return definition

    def ordered_rules(self):
        ordered, visiting, visited = [], set(), set()
        def visit(rule):
            if rule.id in visiting:
                raise ValueError("Rule dependencies cannot cycle")
            if rule.id in visited:
                return
            visiting.add(rule.id)
            for dependency in rule.after:
                if dependency not in self.rules:
                    raise ValueError("Unknown rule dependency")
                predecessor = self.rules[dependency]
                if self.phases.index(predecessor.phase) > self.phases.index(rule.phase):
                    raise ValueError("Rule dependency reverses phase order")
                visit(predecessor)
            visiting.remove(rule.id)
            visited.add(rule.id)
            ordered.append(rule)
        for phase in self.phases:
            same = [self.rules[k] for k in sorted(self.rules) if self.rules[k].phase == phase]
            owners = {}
            for rule in same:
                for key in rule.writes:
                    reducer = rule.reducers.get(key, "exclusive")
                    if key in owners and (reducer == "exclusive" or owners[key] != reducer):
                        raise ValueError(f"Conflicting phase writer: {phase}/{key}")
                    owners[key] = reducer
                visit(rule)
        return tuple(ordered)
