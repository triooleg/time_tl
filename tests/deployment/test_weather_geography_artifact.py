"""Offline preparation tests: exact codes, provenance, bounded public bundle."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "weather_geography_prepare", ROOT / "scripts/deployment/prepare_weather_geography.py"
)
prep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prep)


def code(number):
    return "UA" + str(number).zfill(17)


def catalog():
    return {
        "provider": "alerts.in.ua",
        "status": "ACTIVE",
        "catalog_version": "synthetic",
        "source_sha256": "b" * 64,
        "rows": [
            {
                "region_id": "10",
                "parent_region_id": None,
                "kind": "OBLAST",
                "name": "Synthetic",
                "katottg": code(1),
                "selectable": True,
            },
            {
                "region_id": "20",
                "parent_region_id": "10",
                "kind": "RAION",
                "name": "Synthetic",
                "katottg": code(2),
                "selectable": True,
            },
            {
                "region_id": "30",
                "parent_region_id": "20",
                "kind": "HROMADA",
                "name": "Same name",
                "katottg": code(3),
                "selectable": True,
            },
            {
                "region_id": "40",
                "parent_region_id": "20",
                "kind": "HROMADA",
                "name": "Same name",
                "katottg": code(4),
                "selectable": True,
            },
            {
                "region_id": "50",
                "parent_region_id": None,
                "kind": "SPECIAL_STATUS_CITY",
                "name": "Special",
                "katottg": None,
                "selectable": False,
            },
        ],
    }


def row(number=3, **changes):
    result = dict.fromkeys(prep.HEADER, "")
    result.update(
        code=code(number),
        name="Unrelated source name",
        level="3",
        category="H",
        parent_code=code(2),
        region_code=code(1),
        raion_code=code(2),
        hromada_code=code(number),
        lat="48.500001",
        lon="35.000001",
        osm_id="12345",
        osm_type="R",
        match="code",
    )
    return {**result, **changes}


def source(*rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=prep.HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def test_exact_identity_hierarchy_and_original_coordinates_no_name_matching_or_uid_math():
    normalized, counts = prep.normalize(source(row(4), row(3)), catalog())
    values = list(csv.DictReader(io.StringIO(normalized.decode())))
    assert [v["katottg"] for v in values] == [code(3), code(4)]
    assert [v["region_id"] for v in values] == ["30", "40"]
    assert all(v["latitude"] == "48.500001" and v["longitude"] == "35.000001" for v in values)
    assert all(v["coverage_type"] == "BASE" for v in values)
    assert counts["normalized_communities"] == 2
    assert list(values[0]) == prep.OUTPUT_HEADER


@pytest.mark.parametrize(
    "changes,counter",
    [
        ({"match": "name"}, "excluded_not_code_match"),
        ({"match": "parent"}, "excluded_not_code_match"),
        ({"match": "none", "lat": "", "lon": ""}, "excluded_not_code_match"),
        ({"raion_code": code(99)}, "excluded_hierarchy_mismatch"),
        ({"region_code": code(99)}, "excluded_hierarchy_mismatch"),
        ({"parent_code": code(99)}, "excluded_hierarchy_mismatch"),
        ({"hromada_code": code(99)}, "excluded_hierarchy_mismatch"),
        ({"lat": "NaN"}, "excluded_invalid_coordinates_or_geometry"),
        ({"lat": "43"}, "excluded_invalid_coordinates_or_geometry"),
        ({"lon": "42"}, "excluded_invalid_coordinates_or_geometry"),
        ({"osm_type": "N"}, "excluded_invalid_coordinates_or_geometry"),
        ({"category": "M"}, "excluded_not_category_h"),
    ],
)
def test_unverified_source_rows_excluded_without_poisoning_other_community(changes, counter):
    normalized, counts = prep.normalize(source(row(3), row(4, **changes)), catalog())
    assert len(list(csv.DictReader(io.StringIO(normalized.decode())))) == 1
    assert counts[counter] == 1


def test_unknown_code_same_name_never_inherits_other_provider_uid():
    normalized, counts = prep.normalize(source(row(3), row(99, name="Same name")), catalog())
    assert counts["excluded_unknown_or_unselectable_community"] == 1
    assert b",30," in normalized and b"99" not in normalized


@pytest.mark.parametrize("change", ["orphan", "cycle", "kind", "disabled_parent", "missing_code"])
def test_broken_catalogue_parent_path_never_used(change):
    data = catalog()
    if change == "orphan":
        data["rows"][3]["parent_region_id"] = "999"
    if change == "cycle":
        data["rows"][3]["parent_region_id"] = "40"
    if change == "kind":
        data["rows"][3]["kind"] = "SPECIAL_STATUS_CITY"
    if change == "disabled_parent":
        data["rows"][3]["selectable"] = False
    if change == "missing_code":
        data["rows"][3]["katottg"] = None
    normalized, counts = prep.normalize(source(row(3), row(4)), data)
    assert counts["normalized_communities"] == 1 and b",40," not in normalized


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate_key",
        "extra",
        "bool_selectable",
        "duplicate_uid",
        "duplicate_code",
        "wrong_provider",
        "inactive",
        "deep",
    ],
)
def test_invalid_catalogue_rejected(kind):
    data = catalog()
    if kind == "duplicate_key":
        raw = b'{"provider":"x","provider":"y"}'
    elif kind == "deep":
        raw = b"[" * 2000 + b"0" + b"]" * 2000
    else:
        if kind == "extra":
            data["private_payload"] = "forbidden"
        if kind == "bool_selectable":
            data["rows"][0]["selectable"] = 1
        if kind == "duplicate_uid":
            data["rows"][1]["region_id"] = "10"
        if kind == "duplicate_code":
            data["rows"][1]["katottg"] = code(1)
        if kind == "wrong_provider":
            data["provider"] = "guessed"
        if kind == "inactive":
            data["status"] = "RETIRED"
        raw = json.dumps(data).encode()
    with pytest.raises(prep.GeographyRejected):
        prep.parse_catalog(raw)


def test_duplicate_or_changed_source_schema_rejected():
    for content in (source(row(3), row(3)), source(row(3)).replace(b"code,name", b"wrong,name", 1)):
        with pytest.raises(prep.GeographyRejected):
            prep.normalize(content, catalog())


def prepared(tmp_path, monkeypatch, directory="artifact"):
    upstream = source(row(3), row(4, match="none", lat="", lon=""))
    license_bytes = b"synthetic licence fixture; no upstream authority claim\n"
    captured = (json.dumps(catalog(), sort_keys=True) + "\n").encode()
    monkeypatch.setattr(prep, "SOURCE_SHA256", prep.digest(upstream))
    monkeypatch.setattr(prep, "LICENSE_SHA256", prep.digest(license_bytes))
    monkeypatch.setattr(prep, "CATALOG_SHA256", prep.digest(captured))
    monkeypatch.setattr(prep, "CATALOG_VERSION", "synthetic")
    monkeypatch.setattr(prep, "CATALOG_SOURCE_SHA256", "b" * 64)
    args = dict(
        source=upstream,
        upstream_license=license_bytes,
        catalog_bytes=captured,
        procedure=b"# synthetic converter bytes\n",
        output=tmp_path / directory,
    )
    return args


def test_preparation_bounds_all_inputs_and_writes_nothing_on_bad_pin(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    args["source"] += b"changed"
    with pytest.raises(prep.GeographyRejected):
        prep.prepare(**args)
    assert not args["output"].exists()


def test_deterministic_public_derivative_bundle_manifest_and_intact_licence(tmp_path, monkeypatch):
    args = prepared(tmp_path, monkeypatch)
    first = prep.prepare(**args)
    second = prep.prepare(**{**args, "output": tmp_path / "reproduced"})
    assert first == second and first["status"] == "PREPARED"
    assert not first["production_imported"] and not first["production_enabled"]
    root = args["output"]
    manifest = json.loads((root / "geography-manifest.json").read_text())
    assert (root / "geography-manifest.json").stat().st_size <= 16384
    assert (root / "geography.csv").stat().st_size <= 2_000_000
    assert manifest["license"] == "ODbL-1.0" and manifest["coordinate_kind"] == prep.COORDINATE_KIND
    assert manifest["publisher_commit"] == prep.COMMIT
    assert manifest["procedure_sha256"] == prep.digest(args["procedure"])
    assert (root / "UPSTREAM-LICENSE.txt").read_bytes() == args["upstream_license"]
    assert "OpenStreetMap" in (root / "ATTRIBUTION.md").read_text()
    with zipfile.ZipFile(root / "geography-bundle.zip") as archive:
        assert set(archive.namelist()) == set(manifest["published_files"]) | {
            "geography-manifest.json"
        }
        assert set(manifest["published_files"]) == set(manifest["files"])
        assert "active-catalog.json" not in archive.namelist()
        assert "upstream-katottg.csv" not in archive.namelist()
        assert manifest["operational_catalog_redistributed"] is False
        assert archive.testzip() is None
        for name, sha in manifest["files"].items():
            assert prep.digest(archive.read(name)) == sha
            assert archive.read(name) == (root / name).read_bytes()
            assert archive.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0)
    with pytest.raises(prep.GeographyRejected):
        prep.prepare(**args)


def test_fetch_never_accepts_unknown_url_or_provider_api(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(prep.urllib.request, "urlopen", forbidden)
    with pytest.raises(prep.GeographyRejected):
        prep.fetch("https://api.met.no/", "a" * 64, 100)


def test_real_prepared_artifact_is_pinned_reproducible_and_has_no_guessed_bindings(tmp_path):
    inputs = ROOT / ".release/weather-geo-20261105/geography"
    folder = ROOT / ".release/weather-geo-20261105/geography-public"
    if not folder.exists():
        pytest.skip("public release artifact absent; synthetic preparation tests remain available")
    result = prep.prepare(
        source=(inputs / "upstream-katottg.csv").read_bytes(),
        upstream_license=(folder / "UPSTREAM-LICENSE.txt").read_bytes(),
        catalog_bytes=(inputs / "active-catalog.json").read_bytes(),
        procedure=(ROOT / "scripts/deployment/prepare_weather_geography.py").read_bytes(),
        output=tmp_path / "reproduced",
    )
    for filename in ("geography.csv", "geography-manifest.json", "geography-bundle.zip"):
        assert (tmp_path / "reproduced" / filename).read_bytes() == (folder / filename).read_bytes()
    assert result["coverage_counts"]["normalized_communities"] == 1467
    assert result["coverage_counts"]["source_missing_coordinates"] == 303
    rows = list(csv.DictReader(io.StringIO((folder / "geography.csv").read_text())))
    bindings = {row["katottg"]: row["region_id"] for row in rows}
    assert bindings["UA12040150000015189"] == "300"  # Public community identity, no user lookup.
    assert "UA59080130000022249" not in bindings
    assert "UA59080150000013842" not in bindings
