"""Finite typed effects. Only the frame reducer owns authoritative mutations."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Transfer:
    source: str
    lot_id: str
    target: str
    quantity: int
    slot: str = "contained"
    bound_to: str | None = None


@dataclass(frozen=True)
class Source:
    target: str
    material: str
    quantity: int
    slot: str
    reason: str
    bound_to: str | None = None


@dataclass(frozen=True)
class Sink:
    source: str
    lot_id: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class Damage:
    target: str
    amount: int


@dataclass(frozen=True)
class SetBurning:
    target: str
    lot_id: str
    burning: bool


@dataclass(frozen=True)
class Break:
    target: str


@dataclass(frozen=True)
class Move:
    target: str
    x: int
    y: int


@dataclass(frozen=True)
class SpillTransfer:
    reservoir: str
    target: str
    quantity: int
    slot: str = "absorbed"
    bound_to: str | None = None


@dataclass(frozen=True)
class SpillSink:
    reservoir: str
    quantity: int
    reason: str = "drain"
