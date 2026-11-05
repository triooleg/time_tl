"""Transient address candidates; no address is a persisted power subscription."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .location import PowerLocationSeed
from .models import PowerProvider, validate_group


def public_text(value: object, *, maximum: int = 256, empty: bool = False) -> str:
    if (type(value) is not str or not (0 if empty else 1) <= len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid_power_address_value")
    return value


def katottg(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"UA[0-9]{17}", value) is None:
        raise ValueError("invalid_power_address_territory")
    return value


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class SettlementChoice:
    katottg: str
    name: str
    seed_katottg: str
    catalog_version: str
    area_katottg: str
    area_slug: str = ""

    def __post_init__(self) -> None:
        for value in (self.katottg, self.seed_katottg, self.area_katottg):
            katottg(value)
        public_text(self.name)
        public_text(self.catalog_version, maximum=100)
        public_text(self.area_slug, maximum=128, empty=True)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class StreetChoice:
    street_id: str
    label: str
    settlement: SettlementChoice

    def __post_init__(self) -> None:
        if (type(self.street_id) is not str
                or re.fullmatch(r"[a-fA-F0-9]{24}", self.street_id) is None):
            raise ValueError("invalid_power_address_street")
        public_text(self.label)
        if type(self.settlement) is not SettlementChoice:
            raise ValueError("invalid_power_address_chain")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class HouseChoice:
    value: str
    number: str
    building: str
    label: str
    street: StreetChoice

    def __post_init__(self) -> None:
        public_text(self.value, maximum=512)
        public_text(self.number, maximum=80)
        public_text(self.building, maximum=80, empty=True)
        public_text(self.label)
        if type(self.street) is not StreetChoice:
            raise ValueError("invalid_power_address_chain")


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class AssignmentChoice:
    assignment_id: str
    provider: PowerProvider
    group_id: str
    label: str

    def __post_init__(self) -> None:
        public_text(self.assignment_id, maximum=160)
        public_text(self.label)
        validate_group(self.group_id)
        if not isinstance(self.provider, PowerProvider):
            raise ValueError("invalid_power_address_provider")


class PowerAddressDirectory(Protocol):
    async def settlements(self, seed: PowerLocationSeed, q: str = "") \
            -> tuple[SettlementChoice, ...]: ...

    async def streets(self, seed: PowerLocationSeed, settlement: SettlementChoice, q: str) \
            -> tuple[StreetChoice, ...]: ...

    async def houses(self, street: StreetChoice) -> tuple[HouseChoice, ...]: ...

    async def assignments(self, seed: PowerLocationSeed, settlement: SettlementChoice,
                          street: StreetChoice, house: HouseChoice) \
            -> tuple[AssignmentChoice, ...]: ...
