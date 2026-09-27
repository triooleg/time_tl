"""Validated alerts.in.ua catalogue with fail-closed hierarchy wiring.

The upstream Google Sheet is linked from the official alerts.in.ua API docs.
It lists every UID, but its CSV export has no parent UID columns. Row order is
not sufficient to recover the hierarchy unambiguously, so this module exposes
the verified flat catalogue while deliberately refusing production wiring.
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import NoReturn

SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/edit?usp=sharing"
)
EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/export?format=csv&gid=0"
)
RETRIEVED_ON = "2026-09-20"
EXPECTED_SHA256 = "a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620"
EXPECTED_RECORD_COUNT = 1622
EXPECTED_TOP_LEVEL_COUNT = 27
ALLOWED_TYPES = frozenset({"Область", "Місто з спеціальним статусом", "Район", "Громада"})
TOP_LEVEL_TYPES = frozenset({"Область", "Місто з спеціальним статусом"})
_REPOSITORY_DATA_DIR = Path(__file__).parents[3] / "data"
DATA_PATH = (
    Path(os.environ.get("TIME_TL_DATA_DIR", _REPOSITORY_DATA_DIR))
    / "alerts_in_ua"
    / "locations.csv"
)

# Runtime composition must check this before using the catalogue for alert
# propagation. It remains false until an official parent mapping is available.
WIRE_RUNTIME = False


class RegistryValidationError(RuntimeError):
    """The pinned official export no longer satisfies its reviewed contract."""


class HierarchyUnavailableError(RuntimeError):
    """The flat catalogue cannot safely be turned into alert scopes."""


@dataclass(frozen=True)
class LocationRecord:
    uid: str
    name_uk: str
    location_type: str
    notes: str
    source_row: int


@dataclass(frozen=True)
class FullRegionCatalog:
    records: tuple[LocationRecord, ...]
    sha256: str

    @property
    def by_uid(self) -> dict[str, LocationRecord]:
        return {record.uid: record for record in self.records}

    @property
    def top_level(self) -> tuple[LocationRecord, ...]:
        return tuple(record for record in self.records if record.location_type in TOP_LEVEL_TYPES)


def load_catalog(path: Path = DATA_PATH) -> FullRegionCatalog:
    """Load and strictly validate the byte-pinned official CSV export."""

    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RegistryValidationError(
            f"location export SHA256 mismatch: expected {EXPECTED_SHA256}, got {digest}"
        )

    rows = list(csv.reader(StringIO(payload.decode("utf-8-sig"))))
    try:
        header_index = next(
            index
            for index, row in enumerate(rows)
            if len(row) >= 4 and row[:4] == ["UID", "Назва", "Тип", "Примітки"]
        )
    except StopIteration as error:
        raise RegistryValidationError("official UID header was not found") from error

    records: list[LocationRecord] = []
    for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not row or not row[0].strip():
            continue
        if len(row) < 4:
            raise RegistryValidationError(f"source row {row_index} has fewer than four columns")
        uid, name, location_type, notes = (value.strip() for value in row[:4])
        if not uid.isdecimal() or not name:
            raise RegistryValidationError(f"source row {row_index} has invalid UID or name")
        if location_type not in ALLOWED_TYPES:
            raise RegistryValidationError(
                f"source row {row_index} has unknown type {location_type!r}"
            )
        records.append(LocationRecord(uid, name, location_type, notes, row_index))

    if len(records) != EXPECTED_RECORD_COUNT:
        raise RegistryValidationError(
            f"expected {EXPECTED_RECORD_COUNT} records, got {len(records)}"
        )
    uids = [record.uid for record in records]
    if len(set(uids)) != len(uids):
        raise RegistryValidationError("location UIDs are not unique")
    top_level_count = sum(record.location_type in TOP_LEVEL_TYPES for record in records)
    if top_level_count != EXPECTED_TOP_LEVEL_COUNT:
        raise RegistryValidationError(
            f"expected {EXPECTED_TOP_LEVEL_COUNT} top-level locations, got {top_level_count}"
        )
    return FullRegionCatalog(tuple(records), digest)


def assert_row_order_hierarchy_is_unambiguous(catalog: FullRegionCatalog) -> None:
    """Reject the current export when row order cannot prove parentage."""

    pending_top_level: list[LocationRecord] = []
    for record in catalog.records:
        if record.location_type in TOP_LEVEL_TYPES:
            pending_top_level.append(record)
            continue
        if record.location_type == "Район":
            if len(pending_top_level) > 1:
                rows = ", ".join(str(item.source_row) for item in pending_top_level)
                raise HierarchyUnavailableError(
                    "official export has no parent UID and row order is ambiguous: "
                    f"top-level source rows {rows} precede district row {record.source_row}"
                )
            pending_top_level.clear()


def wire_runtime(path: Path = DATA_PATH) -> NoReturn:
    """Production integration hook; fail closed until hierarchy is official."""

    catalog = load_catalog(path)
    assert_row_order_hierarchy_is_unambiguous(catalog)
    raise HierarchyUnavailableError(
        "official catalogue is flat; reviewed parent mappings are required before wiring"
    )
