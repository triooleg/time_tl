"""Explicit, checksum-bound import of independently reviewed static weather points.

This module never fetches coordinates and never derives a point from a name,
user GPS or an oblast centre. An approved source dataset is an operator input.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import ActiveTerritoryCatalog, AuditLog, Territory, WeatherLocation


@dataclass(frozen=True)
class ReviewedWeatherDataset:
    source_url: str
    sha256: str
    version: str
    reviewed_by: str
    reviewed_at: datetime

    def validate(self, content: bytes) -> None:
        url = urlsplit(self.source_url)
        if (
            not 0 < len(content) <= 10_000_000
            or url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not re.fullmatch(r"[a-f0-9]{64}", self.sha256)
            or hashlib.sha256(content).hexdigest() != self.sha256
            or not 1 <= len(self.version) <= 80
            or not 1 <= len(self.reviewed_by) <= 80
            or self.reviewed_at.tzinfo is None
            or any(ord(char) < 32 for char in self.version + self.reviewed_by)
        ):
            raise ValueError("weather_dataset_not_reviewed")


@dataclass(frozen=True)
class ImportedWeatherPoint:
    katottg: str
    region_id: str
    latitude: float
    longitude: float
    coverage_type: str


def parse_weather_dataset(
    content: bytes, approval: ReviewedWeatherDataset
) -> tuple[ImportedWeatherPoint, ...]:
    approval.validate(content)
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
    if reader.fieldnames != ["katottg", "region_id", "latitude", "longitude", "coverage_type"]:
        raise ValueError("invalid_weather_dataset_columns")
    result: list[ImportedWeatherPoint] = []
    seen: set[tuple[str, str, float, float]] = set()
    bases: set[str] = set()
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("invalid_weather_dataset_row")
        lat, lon = float(row["latitude"]), float(row["longitude"])
        code, region, kind = row["katottg"], row["region_id"], row["coverage_type"]
        identity = (code, kind, lat, lon)
        if (
            not re.fullmatch(r"UA[0-9]{17}", code)
            or not 1 <= len(region) <= 64
            or not all(math.isfinite(v) for v in (lat, lon))
            or not 44 <= lat <= 53.5
            or not 22 <= lon <= 41
            or kind not in {"BASE", "EXTENDED"}
            or identity in seen
            or kind == "BASE"
            and code in bases
            or len(result) >= 100_000
        ):
            raise ValueError("invalid_weather_dataset_point")
        seen.add(identity)
        if kind == "BASE":
            bases.add(code)
        result.append(ImportedWeatherPoint(code, region, lat, lon, kind))
    if not result:
        raise ValueError("empty_weather_dataset")
    return tuple(result)


async def import_weather_locations(
    factory: async_sessionmaker[AsyncSession], content: bytes, approval: ReviewedWeatherDataset
) -> int:
    points = parse_weather_dataset(content, approval)
    async with factory() as session, session.begin():
        # Exact active catalog version, selected under a lock shared by territory
        # activation. All identities are validated before any insert/update.
        active = (
            await session.execute(
                select(ActiveTerritoryCatalog)
                .where(ActiveTerritoryCatalog.provider == "alerts.in.ua")
                .with_for_update()
            )
        ).scalar_one_or_none()
        if active is None:
            raise ValueError("weather_active_catalog_missing")
        rows = (
            (
                await session.execute(
                    select(Territory).where(
                        and_(
                            Territory.provider == active.provider,
                            Territory.catalog_version == active.catalog_version,
                            Territory.external_code.in_([p.katottg for p in points]),
                            Territory.is_selectable.is_(True),
                            Territory.kind.in_(("HROMADA", "SPECIAL_STATUS_CITY")),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        by_code = {row.external_code: row for row in rows}
        if len(by_code) != len(rows) or any(
            point.katottg not in by_code or by_code[point.katottg].region_id != point.region_id
            for point in points
        ):
            raise ValueError("weather_dataset_catalog_identity_mismatch")
        for point in points:
            existing = (
                await session.execute(
                    select(WeatherLocation)
                    .where(
                        WeatherLocation.katottg_code == point.katottg,
                        WeatherLocation.coverage_type == point.coverage_type,
                        WeatherLocation.enabled.is_(True),
                        *(
                            []
                            if point.coverage_type == "BASE"
                            else [
                                WeatherLocation.latitude == point.latitude,
                                WeatherLocation.longitude == point.longitude,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None and (
                existing.latitude != point.latitude
                or existing.longitude != point.longitude
                or existing.region_id != point.region_id
                or existing.provenance_url != approval.source_url
                or existing.provenance_sha256 != approval.sha256
                or existing.provenance_version != approval.version
            ):
                # Forecast/cache identity is immutable. An old in-flight result
                # remains attached to the disabled old UUID, never the new point.
                existing.enabled = False
                existing.coverage_type = "EXTENDED"
                await session.flush()  # Release BASE unique key before replacement.
                existing = None
            if existing is None:
                existing = WeatherLocation(id=uuid4())
                session.add(existing)
            existing.katottg_code, existing.region_id = point.katottg, point.region_id
            existing.locality_name = by_code[point.katottg].name
            existing.latitude, existing.longitude = point.latitude, point.longitude
            existing.coverage_type, existing.enabled = point.coverage_type, True
            existing.provenance_url, existing.provenance_sha256 = (
                approval.source_url,
                approval.sha256,
            )
            existing.provenance_version, existing.approved_at = (
                approval.version,
                approval.reviewed_at,
            )
        session.add(
            AuditLog(
                actor=approval.reviewed_by,
                action="weather_locations_imported",
                result="RECORDED",
                target_type="weather_dataset",
                target_id=approval.sha256,
                redacted_details={"point_count": len(points), "version": approval.version},
            )
        )
    return len(points)
