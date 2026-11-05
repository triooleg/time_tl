"""Reuse confirmed geography without confusing alert IDs with settlement codes."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerLocationSeed:
    katottg: str
    territory_kind: str
    label: str
    catalog_version: str

    def __post_init__(self) -> None:
        if (
            type(self.katottg) is not str
            or re.fullmatch(r"UA[0-9]{17}", self.katottg) is None
            or self.territory_kind not in {"OBLAST", "RAION", "HROMADA", "SPECIAL_STATUS_CITY"}
            or type(self.label) is not str
            or not 1 <= len(self.label) <= 256
            or type(self.catalog_version) is not str
            or not 1 <= len(self.catalog_version) <= 100
            or any(ord(char) < 32 for char in self.label + self.catalog_version)
        ):
            raise ValueError("power_territory_mapping_unavailable")

    @property
    def needs_settlement(self) -> bool:
        # A community may contain several settlements. Never guess the city from
        # its name, nor pass a community/provider UID as a settlement KATOTTG.
        return self.territory_kind != "SPECIAL_STATUS_CITY"
