"""Prepare a checksum-bound, public, code-exact weather geography derivative.

LOCAL artifacts only. No database, provider API, geocoder, Telegram, deployment,
signing, credentials or inferred coordinates. Independent review remains required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

COMMIT = "d37d5e967e3bb45255d180ea740e5a6ca90e00cc"
SOURCE_URL = f"https://raw.githubusercontent.com/alexbabintsev/ua-geo/{COMMIT}/data/out/katottg.csv"
SOURCE_SHA256 = "0952de2b19b1fed78b041cac415f296c73b663649e1325f0823082c4f7db1024"
LICENSE_URL = f"https://raw.githubusercontent.com/alexbabintsev/ua-geo/{COMMIT}/LICENSE"
LICENSE_SHA256 = "d894616f4124d562d6604fa3ca1110bac8782e43ac3ed831263eed30414f9130"
CATALOG_SHA256 = "9c6b2a95af64031e0155dc8e15192c83064176c6f2f94d543b88186904cfe1ca"
CATALOG_VERSION = "alerts-in-ua-katottg-4.0.0-20260924-41d18068dd1a"
CATALOG_SOURCE_SHA256 = "ea1d3ba0f1642a6e987d974d917478183dfc1215edfea4f7dfe8fef47af73fbe"
COORDINATE_KIND = "approximate-community-representative"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
HEADER = [
    "code",
    "name",
    "full_name",
    "level",
    "category",
    "category_name",
    "parent_code",
    "region_code",
    "raion_code",
    "hromada_code",
    "koatuu",
    "lat",
    "lon",
    "area_km2",
    "osm_id",
    "osm_type",
    "match",
]
OUTPUT_HEADER = ["katottg", "region_id", "latitude", "longitude", "coverage_type"]
CATALOG_FIELDS = {"catalog_version", "provider", "rows", "source_sha256", "status"}
ROW_FIELDS = {"katottg", "kind", "name", "parent_region_id", "region_id", "selectable"}
NOTICE = f"""# Attribution and licence notice

Contains information from OpenStreetMap contributors and the ua-geo database,
made available under the Open Database License (ODbL) 1.0.

OpenStreetMap: https://www.openstreetmap.org/copyright
ua-geo publisher: https://github.com/alexbabintsev/ua-geo
Publisher commit: {COMMIT}
Upstream data: {SOURCE_URL}
Data licence: {ODBL_URL}

The normalized geography.csv derivative is made available under ODbL 1.0.
The adjacent downloadable bundle contains its machine-readable data, provenance,
conversion procedure and intact upstream licence notice. Original input datasets
are not redistributed or relicensed by this bundle. No access credential,
user account, user coordinate, address, notification setting or forecast is included.

Time TL changes: retain only code-exact community records, verify their complete
administrative hierarchy against the captured active public territory catalogue,
add the exact provider UID and BASE classification, and select the required columns.
Coordinates are copied from published values; none are computed or geocoded.
These are approximate community representative points, not user locations,
guaranteed administrative centres, official warnings or government coordinate approval.

The source register is published by Ukraine's Ministry responsible for communities:
https://mindev.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad
The provider UID cross-reference is from the captured alerts.in.ua public catalogue;
its source/version/hash are recorded without inventing or transforming provider IDs.
That operational input catalogue remains under its original terms and is not
redistributed in this bundle. The ODbL grant here applies to the normalized
geographic derivative, not to the original catalogue or upstream software.
No publisher endorses Time TL by virtue of this attribution.

Prepared for independent provenance, geographic and licence review; publication
and production import have not been performed by this preparation procedure.
"""
PROCEDURE = """# Reproduce the geography derivative

The converter is prepare_weather_geography.py, copied byte-for-byte alongside
the artifact and bound by procedure_sha256 in geography-manifest.json.
It uses Python's standard library. No database or secret is needed.

Reproduction requires the separately obtained pinned source CSV and an authorized
local operational catalogue capture (not distributed in this public bundle):

    python prepare_weather_geography.py --catalog active-catalog.json \
      --source upstream-katottg.csv --upstream-license UPSTREAM-LICENSE.txt \
      --output NEW_EMPTY_DIRECTORY

The output directory must not exist. The converter verifies fixed source,
licence and catalogue byte hashes, the catalogue version/provider/status, strict
CSV/JSON schemas and unique identifiers. It keeps only level3/categoryH,
match=code, source coordinates within the accepted Ukraine bounds, and a complete
selectable OBLAST -> RAION -> HROMADA exact-code path. All three source hierarchy
codes and the immediate parent must match. No names, nearest locations,
user GPS, oblast centres or UID arithmetic participate in selection.

The normalized CSV is UTF-8 with LF newlines, sorted by KATOTTG; coordinate
strings are copied without calculation. BASE is an application coverage label,
not a claim of address precision. Unknown, unmatched and special-city records
are excluded and counted. The manifest has no self/bundle-hash cycle; the outer
release receipt binds the resulting ZIP separately.

The ZIP has fixed timestamps/modes and contains exactly the public files listed
in the manifest plus the manifest itself. It deliberately excludes the original
CSV and operational catalogue capture.
Input hashes identify them without republishing them. The complete normalized
derivative database is geography.csv. Upstream code/data licences are
distinct: MIT for upstream software; ODbL1.0 for OSM-derived geographic data.
The governing ODbL URI is supplied in LICENSE.txt (ODbL4.2b permits the URI).
The original upstream licence notice is included unchanged.

No data is imported or enabled. An independent reviewer must assess provenance,
geographic suitability, catalogue redistribution rights and ODbL attribution /
derivative availability before an integrator imports or publishes the bundle.
"""


class GeographyRejected(Exception):
    pass


def require(condition: object) -> None:
    if not condition:
        raise GeographyRejected("geography_input_rejected")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def parse_catalog(content: bytes) -> dict[str, Any]:
    require(0 < len(content) <= 2_000_000)
    try:
        value = json.loads(
            content,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(GeographyRejected()),
        )
    except (ValueError, RecursionError) as error:
        raise GeographyRejected("invalid_geography_catalog") from error
    require(type(value) is dict and set(value) == CATALOG_FIELDS)
    require(value["provider"] == "alerts.in.ua" and value["status"] == "ACTIVE")
    require(type(value["catalog_version"]) is str and 1 <= len(value["catalog_version"]) <= 96)
    require(
        type(value["source_sha256"]) is str and re.fullmatch("[0-9a-f]{64}", value["source_sha256"])
    )
    rows = value["rows"]
    require(type(rows) is list and 0 < len(rows) <= 5000)
    ids, codes = set(), set()
    for row in rows:
        require(type(row) is dict and set(row) == ROW_FIELDS)
        identifier, code, parent = row["region_id"], row["katottg"], row["parent_region_id"]
        require(
            type(identifier) is str
            and re.fullmatch(r"[0-9]{1,64}", identifier)
            and identifier not in ids
        )
        require(code is None or type(code) is str and re.fullmatch(r"UA[0-9]{17}", code))
        require(code is None or code not in codes)
        require(parent is None or type(parent) is str and re.fullmatch(r"[0-9]{1,64}", parent))
        require(type(row["selectable"]) is bool)
        require(row["kind"] in {"OBLAST", "RAION", "HROMADA", "SPECIAL_STATUS_CITY"})
        require(
            type(row["name"]) is str
            and 1 <= len(row["name"]) <= 200
            and not any(ord(char) < 32 for char in row["name"])
        )
        ids.add(identifier)
        if code is not None:
            codes.add(code)
    return value


def normalize(source: bytes, catalog: dict[str, Any]) -> tuple[bytes, dict[str, int]]:
    """Pure strict-code join. Publication additionally requires pinned input hashes."""
    require(0 < len(source) <= 10_000_000)
    try:
        reader = csv.DictReader(io.StringIO(source.decode("utf-8-sig")), strict=True)
        require(reader.fieldnames == HEADER)
        records = list(reader)
    except (UnicodeError, csv.Error) as error:
        raise GeographyRejected("invalid_geography_csv") from error
    require(0 < len(records) <= 40000)
    by_id = {row["region_id"]: row for row in catalog["rows"]}
    by_code = {row["katottg"]: row for row in catalog["rows"] if row["katottg"] is not None}
    counts = Counter(
        source_records=len(records),
        catalog_records=len(catalog["rows"]),
        catalog_hromadas=sum(row["kind"] == "HROMADA" for row in catalog["rows"]),
        catalog_special_cities=sum(row["kind"] == "SPECIAL_STATUS_CITY" for row in catalog["rows"]),
        catalog_unmapped=sum(row["katottg"] is None for row in catalog["rows"]),
    )
    selected = []
    seen = set()
    for row in records:
        require(None not in row and all(value is not None for value in row.values()))
        if row["level"] != "3":
            counts["excluded_not_level3"] += 1
            continue
        counts["source_level3"] += 1
        require(re.fullmatch(r"UA[0-9]{17}", row["code"]) and row["code"] not in seen)
        seen.add(row["code"])
        if row["category"] != "H":
            counts["excluded_not_category_h"] += 1
            continue
        if row["match"] != "code":
            counts["excluded_not_code_match"] += 1
            if not row["lat"] or not row["lon"]:
                counts["source_missing_coordinates"] += 1
            continue
        counts["source_exact_code_communities"] += 1
        if (
            not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,9})?", row["lat"])
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,9})?", row["lon"])
            or not row["osm_id"].isdigit()
            or row["osm_type"] != "R"
        ):
            counts["excluded_invalid_coordinates_or_geometry"] += 1
            continue
        lat, lon = float(row["lat"]), float(row["lon"])
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not 44 <= lat <= 53.5
            or not 22 <= lon <= 41
        ):
            counts["excluded_invalid_coordinates_or_geometry"] += 1
            continue
        community = by_code.get(row["code"])
        if community is None or community["kind"] != "HROMADA" or not community["selectable"]:
            counts["excluded_unknown_or_unselectable_community"] += 1
            continue
        district = by_id.get(community["parent_region_id"])
        region = by_id.get(district["parent_region_id"]) if district is not None else None
        if (
            district is None
            or region is None
            or district["kind"] != "RAION"
            or region["kind"] != "OBLAST"
            or region["parent_region_id"] is not None
            or not district["selectable"]
            or not region["selectable"]
            or len({community["region_id"], district["region_id"], region["region_id"]}) != 3
            or row["hromada_code"] != row["code"]
            or row["parent_code"] != row["raion_code"]
            or row["raion_code"] != district["katottg"]
            or row["region_code"] != region["katottg"]
        ):
            counts["excluded_hierarchy_mismatch"] += 1
            continue
        selected.append((row["code"], community["region_id"], row["lat"], row["lon"], "BASE"))
    selected.sort()
    counts["normalized_communities"] = len(selected)
    require(selected)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(OUTPUT_HEADER)
    writer.writerows(selected)
    content = output.getvalue().encode("utf-8")
    require(len(content) <= 2_000_000)
    return content, dict(sorted(counts.items()))


def fetch(url: str, expected: str, limit: int) -> bytes:
    require(url in {SOURCE_URL, LICENSE_URL})
    request = urllib.request.Request(url, headers={"User-Agent": "TimeTLGeographyPreparation/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        require(response.geturl() == url and response.status == 200)
        content = response.read(limit + 1)
    require(len(content) <= limit and digest(content) == expected)
    return content


def prepare(
    *, source: bytes, upstream_license: bytes, catalog_bytes: bytes, procedure: bytes, output: Path
) -> dict[str, Any]:
    require(not output.exists() and not output.is_symlink())
    require(digest(source) == SOURCE_SHA256 and digest(upstream_license) == LICENSE_SHA256)
    require(digest(catalog_bytes) == CATALOG_SHA256)
    catalog = parse_catalog(catalog_bytes)
    require(
        catalog["catalog_version"] == CATALOG_VERSION
        and catalog["source_sha256"] == CATALOG_SOURCE_SHA256
    )
    normalized, counts = normalize(source, catalog)
    files = {
        "geography.csv": normalized,
        "UPSTREAM-LICENSE.txt": upstream_license,
        "ATTRIBUTION.md": NOTICE.encode(),
        "PROCEDURE.md": PROCEDURE.encode(),
        "LICENSE.txt": (
            "Time TL normalized weather geography derivative\n"
            "Open Database License (ODbL) 1.0\n" + ODBL_URL + "\n"
            "Contains OpenStreetMap contributors / ua-geo information.\n"
            "See ATTRIBUTION.md and intact UPSTREAM-LICENSE.txt.\n"
        ).encode(),
        "prepare_weather_geography.py": procedure,
    }
    manifest = {
        "version": 1,
        "status": "PREPARED_FOR_INDEPENDENT_REVIEW",
        "provider": "alerts.in.ua",
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "publisher_commit": COMMIT,
        "license": "ODbL-1.0",
        "license_url": ODBL_URL,
        "publisher_license_url": LICENSE_URL,
        "publisher_license_sha256": LICENSE_SHA256,
        "coordinate_kind": COORDINATE_KIND,
        "normalized_sha256": digest(normalized),
        "normalized_url": "https://time.yoleotard.com/weather/geography/"
        + digest(normalized)
        + "/geography.csv",
        "license_scope": "normalized-geographic-derivative-only",
        "operational_catalog_redistributed": False,
        "published_files": sorted(files),
        "catalog_version": CATALOG_VERSION,
        "catalog_source_sha256": CATALOG_SOURCE_SHA256,
        "catalog_sha256": CATALOG_SHA256,
        "procedure_sha256": digest(procedure),
        "coverage_counts": counts,
        "files": {name: digest(data) for name, data in sorted(files.items())},
        "production_imported": False,
        "production_enabled": False,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    require(len(manifest_bytes) <= 16384)
    files["geography-manifest.json"] = manifest_bytes
    bundle_buffer = io.BytesIO()
    with zipfile.ZipFile(
        bundle_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    files["geography-bundle.zip"] = bundle_buffer.getvalue()
    # All validation/bundle generation completes before creating the new directory.
    output.mkdir(parents=False, exist_ok=False)
    for name, content in sorted(files.items()):
        (output / name).write_bytes(content)
    return {
        "status": "PREPARED",
        "manifest_sha256": digest(manifest_bytes),
        "normalized_sha256": digest(normalized),
        "bundle_sha256": digest(files["geography-bundle.zip"]),
        "procedure_sha256": digest(procedure),
        "coverage_counts": counts,
        "production_imported": False,
        "production_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--upstream-license", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        source = (
            args.source.read_bytes()
            if args.source
            else fetch(SOURCE_URL, SOURCE_SHA256, 10_000_000)
        )
        licence = (
            args.upstream_license.read_bytes()
            if args.upstream_license
            else fetch(LICENSE_URL, LICENSE_SHA256, 20000)
        )
        result = prepare(
            source=source,
            upstream_license=licence,
            catalog_bytes=args.catalog.read_bytes(),
            procedure=Path(__file__).read_bytes(),
            output=args.output,
        )
    except Exception:
        print(
            json.dumps(
                {"status": "REJECTED", "production_imported": False, "production_enabled": False}
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
