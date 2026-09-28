"""Byte-pinned alerts.in.ua catalogue and official row-order hierarchy.

The upstream Google Sheet is linked from the official alerts.in.ua API docs.
Provider UID, label, type, source order and the parent derived from that order
are authoritative. KATOTTG may validate or enrich but never replace this tree.
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass, replace
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
EXPECTED_PARENT_LINK_COUNT = 1595
ALLOWED_TYPES = frozenset({"Область", "Місто з спеціальним статусом", "Район", "Громада"})
TOP_LEVEL_TYPES = frozenset({"Область", "Місто з спеціальним статусом"})
_REPOSITORY_DATA_DIR = Path(__file__).parents[3] / "data"
DATA_PATH = (
    Path(os.environ.get("TIME_TL_DATA_DIR", _REPOSITORY_DATA_DIR))
    / "alerts_in_ua"
    / "locations.csv"
)

# The all-territory IoT wire remains closed because it emits active positions
# absent from the official UID list.  The official active.json endpoint carries
# explicit UIDs and may be wired only through its separately reviewed adapter.
WIRE_RUNTIME = False
ACTIVE_JSON_WIRE_RUNTIME = True


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
    parent_uid: str | None = None


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

    @property
    def parent_link_count(self) -> int:
        return sum(record.parent_uid is not None for record in self.records)


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
    catalog = FullRegionCatalog(_derive_row_order_hierarchy(records), digest)
    assert_row_order_hierarchy_is_unambiguous(catalog)
    return catalog


def _derive_row_order_hierarchy(
    records: list[LocationRecord],
) -> tuple[LocationRecord, ...]:
    result: list[LocationRecord] = []
    current_oblast_uid: str | None = None
    current_raion_uid: str | None = None
    pending_roots: list[LocationRecord] = []

    for record in records:
        if record.location_type in TOP_LEVEL_TYPES:
            pending_roots.append(record)
            current_raion_uid = None
            result.append(record)
            continue

        if pending_roots:
            root_uids = tuple(item.uid for item in pending_roots)
            if len(pending_roots) == 1:
                root = pending_roots[0]
                if root.location_type != "Область":
                    raise RegistryValidationError(
                        f"district block follows non-oblast root UID {root.uid}"
                    )
                current_oblast_uid = root.uid
            elif root_uids == ("31", "30", "8", "29"):
                # Exact reviewed official prologue. Kyiv, Sevastopol and Crimea
                # are childless roots; the following raion block is Volyn.
                current_oblast_uid = "8"
            else:
                raise RegistryValidationError(
                    f"ambiguous consecutive root run {root_uids!r}"
                )
            pending_roots.clear()

        if record.location_type == "Район":
            if current_oblast_uid is None:
                raise RegistryValidationError(
                    f"orphan raion UID {record.uid} at source row {record.source_row}"
                )
            current_raion_uid = record.uid
            result.append(replace(record, parent_uid=current_oblast_uid))
        elif record.location_type == "Громада":
            if current_raion_uid is None:
                raise RegistryValidationError(
                    f"orphan hromada UID {record.uid} at source row {record.source_row}"
                )
            result.append(replace(record, parent_uid=current_raion_uid))
        else:
            raise RegistryValidationError(
                f"source row {record.source_row} has unknown type {record.location_type!r}"
            )
    return tuple(result)


def assert_row_order_hierarchy_is_unambiguous(catalog: FullRegionCatalog) -> None:
    """Validate every provider parent and the complete 27-root tree."""

    by_uid = catalog.by_uid
    canonical = {
        record.uid: record.parent_uid
        for record in _derive_row_order_hierarchy(
            [replace(record, parent_uid=None) for record in catalog.records]
        )
    }
    for record in catalog.records:
        if record.parent_uid != canonical[record.uid]:
            raise RegistryValidationError(
                f"UID {record.uid} disagrees with canonical provider row order"
            )
        if record.location_type in TOP_LEVEL_TYPES:
            if record.parent_uid is not None:
                raise RegistryValidationError(f"top-level UID {record.uid} has a parent")
            continue
        if record.parent_uid is None:
            raise RegistryValidationError(f"non-root UID {record.uid} has no parent")
        parent = by_uid.get(record.parent_uid)
        if parent is None:
            raise RegistryValidationError(
                f"UID {record.uid} references missing parent {record.parent_uid}"
            )
        expected = "Область" if record.location_type == "Район" else "Район"
        if parent.location_type != expected or parent.source_row >= record.source_row:
            raise RegistryValidationError(f"UID {record.uid} has an invalid parent")
    if catalog.parent_link_count != EXPECTED_PARENT_LINK_COUNT:
        raise RegistryValidationError(
            f"expected {EXPECTED_PARENT_LINK_COUNT} parent links, "
            f"got {catalog.parent_link_count}"
        )


def wire_runtime(path: Path = DATA_PATH) -> NoReturn:
    """Production integration hook; fail closed on unpublished IoT UIDs."""

    catalog = load_catalog(path)
    assert_row_order_hierarchy_is_unambiguous(catalog)
    raise HierarchyUnavailableError(
        "official hierarchy is valid, but IoT UIDs 1805..1814 are absent "
        "from the official location list"
    )
