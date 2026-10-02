"""Operator-only import of a pre-downloaded official KATOTTG snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path

from time_tl.config import Settings
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.territories import PostgresTerritoryCatalogRepository
from time_tl.runtime.region_registry_full import load_catalog
from time_tl.runtime.territory_catalog_builder import build_territory_catalog


@dataclass(frozen=True, slots=True)
class TerritoryImportResult:
    catalog_version: str
    row_count: int
    selectable_count: int
    hidden_nonselectable_count: int
    unresolved_total: int
    unresolved_uids: tuple[str, ...]
    source_revision: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class TerritoryActivationResult:
    """Sanitized evidence for a separately activated immutable draft."""

    catalog_version: str
    source_sha256: str
    record_count: int
    unresolved_total: int


async def import_territory_catalog(
    settings: Settings,
    *,
    katottg_json: Path,
    catalog_version: str,
) -> TerritoryImportResult:
    """Validate both sources and persist an immutable draft for later review."""

    payload = await asyncio.to_thread(katottg_json.read_bytes)
    built = build_territory_catalog(payload)
    selectable_count = sum(row.is_selectable for row in built.rows)
    hidden_count = len(built.rows) - selectable_count
    report = {
        "alerts_source_sha256": built.alerts_source_sha256,
        "katottg_source_sha256": built.katottg_source_sha256,
        "source_record_count": 1622,
        "persisted_record_count": len(built.rows),
        "selectable_count": selectable_count,
        "hidden_count": hidden_count,
        "omitted_count": 1622 - len(built.rows),
        "unresolved_total": len(built.unresolved),
        "issues": [asdict(item) for item in built.unresolved],
    }
    engine = create_engine(settings.database_url.get_secret_value())
    official_region_ids = frozenset(record.uid for record in load_catalog().records)
    repository = PostgresTerritoryCatalogRepository(
        create_session_factory(engine),
        expected_region_ids=official_region_ids,
    )
    try:
        count = await repository.load_draft(
            provider="alerts.in.ua",
            catalog_version=catalog_version,
            source_revision=built.source_revision,
            source_sha256=built.source_sha256,
            rows=built.rows,
            selectable_count=selectable_count,
            hidden_nonselectable_count=hidden_count,
            unresolved_total=len(built.unresolved),
            reconciliation_report=report,
        )
    finally:
        await engine.dispose()
    return TerritoryImportResult(
        catalog_version=catalog_version,
        row_count=count,
        selectable_count=selectable_count,
        hidden_nonselectable_count=hidden_count,
        unresolved_total=len(built.unresolved),
        unresolved_uids=built.unresolved_uids,
        source_revision=built.source_revision,
        source_sha256=built.source_sha256,
    )


async def activate_territory_catalog(
    settings: Settings,
    *,
    catalog_version: str,
    expected_source_sha256: str,
    expected_persisted_rows: int,
    expected_unresolved_total: int,
) -> TerritoryActivationResult:
    """Activate an existing DRAFT without rebuilding or replacing its rows."""

    catalog_version = catalog_version.strip()
    expected_source_sha256 = expected_source_sha256.strip().lower()
    if not catalog_version:
        raise ValueError("catalog version is required")
    if len(expected_source_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_source_sha256
    ):
        raise ValueError("expected source SHA256 must be 64 lowercase hex digits")
    if expected_persisted_rows <= 0:
        raise ValueError("expected persisted row count must be positive")
    if expected_unresolved_total < 0:
        raise ValueError("expected unresolved count cannot be negative")

    engine = create_engine(settings.database_url.get_secret_value())
    official_region_ids = frozenset(record.uid for record in load_catalog().records)
    repository = PostgresTerritoryCatalogRepository(
        create_session_factory(engine),
        expected_region_ids=official_region_ids,
    )
    try:
        await repository.activate(
            provider="alerts.in.ua",
            catalog_version=catalog_version,
            expected_source_sha256=expected_source_sha256,
            expected_record_count=expected_persisted_rows,
            expected_unresolved_total=expected_unresolved_total,
        )
    finally:
        await engine.dispose()
    return TerritoryActivationResult(
        catalog_version=catalog_version,
        source_sha256=expected_source_sha256,
        record_count=expected_persisted_rows,
        unresolved_total=expected_unresolved_total,
    )
