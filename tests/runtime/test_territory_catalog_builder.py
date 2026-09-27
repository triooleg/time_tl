from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from time_tl.runtime.region_registry_full import FullRegionCatalog, LocationRecord
from time_tl.runtime.territory_catalog_builder import build_territory_catalog


def _record(uid: str, name: str, location_type: str, row: int) -> LocationRecord:
    return LocationRecord(uid, name, location_type, "", row)


def _catalog(*records: LocationRecord) -> FullRegionCatalog:
    ordered = sorted(records, key=lambda item: item.source_row)
    result: list[LocationRecord] = []
    current_oblast: str | None = None
    current_raion: str | None = None
    for record in ordered:
        if record.location_type in {"Область", "Місто з спеціальним статусом"}:
            current_oblast = record.uid if record.location_type == "Область" else None
            current_raion = None
            result.append(record)
        elif record.location_type == "Район":
            assert current_oblast is not None
            current_raion = record.uid
            result.append(replace(record, parent_uid=current_oblast))
        else:
            assert current_raion is not None
            result.append(replace(record, parent_uid=current_raion))
    return FullRegionCatalog(tuple(result), "a" * 64)


def _payload(*terms: dict[str, object]) -> bytes:
    return json.dumps(
        {"agency": "SSSU", "id": "CL_KATOTTG", "version": "4.0.0", "terms": list(terms)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _term(code: str, name: str, category: str, parent: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "id": code,
        "names": {"uk": name},
        "attributes": {"KATOTTG_12": code, "KATOTTG_Category": category},
    }
    if parent is not None:
        result["parent"] = parent
    return result


def test_builds_dnipropetrovsk_kamianske_chain_and_source_evidence() -> None:
    ordered_alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
        _record("300", "м. Кам’янське та Кам’янська територіальна громада", "Громада", 3),
    )
    shuffled_alerts = _catalog(
        _record("300", "м. Кам’янське та Кам’янська територіальна громада", "Громада", 3),
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12040150000015189", "Кам’янська", "H", "UA12040000000032213"),
    )

    result = build_territory_catalog(payload, alerts_catalog=ordered_alerts)
    shuffled = build_territory_catalog(payload, alerts_catalog=shuffled_alerts)

    expected = {
        "9": (None, "UA12000000000090473"),
        "42": ("9", "UA12040000000032213"),
        "300": ("42", "UA12040150000015189"),
    }
    assert {
        row.region_id: (row.parent_region_id, row.external_code) for row in result.rows
    } == expected
    assert {
        row.region_id: (row.parent_region_id, row.external_code) for row in shuffled.rows
    } == expected
    assert [row.region_id for row in result.rows] == ["9", "42", "300"]
    assert [row.region_id for row in shuffled.rows] == ["9", "42", "300"]
    assert all(row.is_selectable for row in result.rows)
    assert result.unresolved_uids == ()
    assert result.alerts_source_sha256 == "a" * 64
    assert result.katottg_source_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.source_revision == "SSSU:CL_KATOTTG(4.0.0)"


def test_same_parent_duplicate_communities_are_retained_but_hidden() -> None:
    alerts = _catalog(
        _record("20", "Сумська область", "Область", 1),
        _record("114", "Сумський район", "Район", 2),
        _record("1180", "Миколаївська територіальна громада", "Громада", 3),
        _record("1181", "Миколаївська територіальна громада", "Громада", 4),
    )
    payload = _payload(
        _term("UA59000000000057109", "Сумська", "O"),
        _term("UA59080000000057892", "Сумський", "P", "UA59000000000057109"),
        _term("UA59080130000022249", "Миколаївська", "H", "UA59080000000057892"),
        _term("UA59080150000013842", "Миколаївська", "H", "UA59080000000057892"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    for uid in ("1180", "1181"):
        assert rows[uid].parent_region_id == "114"
        assert rows[uid].external_code is None
        assert not rows[uid].is_selectable
    assert result.unresolved_uids == ("1180", "1181")


def test_missing_community_fails_closed_and_same_name_is_district_scoped() -> None:
    alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
        _record("301", "Відсутня територіальна громада", "Громада", 3),
        _record("302", "Спільна територіальна громада", "Громада", 4),
        _record("303", "Кам’янська територіальна громада", "Громада", 5),
        _record("99", "Інший район", "Район", 6),
        _record("304", "Інша територіальна громада", "Громада", 7),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12060000000000001", "Інший", "P", "UA12000000000090473"),
        _term("UA12040010000000001", "Спільна", "H", "UA12040000000032213"),
        _term("UA12060010000000001", "Спільна", "H", "UA12060000000000001"),
        _term("UA12040150000015189", "Кам’янська", "H", "UA12040000000032213"),
        _term("UA12060030000000001", "Інша", "H", "UA12060000000000001"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert result.unresolved_uids == ("301",)
    assert not rows["301"].is_selectable
    assert rows["301"].external_code is None
    assert rows["302"].parent_region_id == "42"
    assert rows["302"].external_code == "UA12040010000000001"
    assert rows["303"].parent_region_id == "42"
    assert rows["304"].parent_region_id == "99"
    reasons = {item.uid: item.reason for item in result.unresolved}
    assert reasons["301"] == "missing official community in provider district"


def test_katottg_parent_mismatch_cannot_reparent_provider_hromada() -> None:
    alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
        _record("300", "Спільна територіальна громада", "Громада", 3),
        _record("99", "Інший район", "Район", 4),
        _record("304", "Інша територіальна громада", "Громада", 5),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12060000000000001", "Інший", "P", "UA12000000000090473"),
        _term("UA12060010000000001", "Спільна", "H", "UA12060000000000001"),
        _term("UA12060030000000001", "Інша", "H", "UA12060000000000001"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["300"].parent_region_id == "42"
    assert rows["300"].parent_region_id != "99"
    assert rows["300"].external_code is None
    assert not rows["300"].is_selectable
    assert result.unresolved_uids == ("300",)


def test_city_key_selects_one_exact_official_hromada_descendant() -> None:
    alerts = _catalog(
        _record("300", "м. Кам’янське та Кам’янська територіальна громада", "Громада", 3),
        _record("99", "Інший район", "Район", 4),
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("304", "Інша територіальна громада", "Громада", 5),
        _record("42", "Кам'янський район", "Район", 2),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12060000000000001", "Інший", "P", "UA12000000000090473"),
        _term("UA12040150000015189", "Кам’янська", "H", "UA12040000000032213"),
        _term("UA12060150000000001", "Кам’янська", "H", "UA12060000000000001"),
        _term("UA12040150010000001", "Кам’янське", "M", "UA12040150000015189"),
        _term("UA12060150010000001", "Кам’янське", "C", "UA12060150000000001"),
        _term("UA12060030000000001", "Інша", "H", "UA12060000000000001"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["300"].parent_region_id == "42"
    assert rows["300"].external_code == "UA12040150000015189"
    assert rows["300"].is_selectable


def test_city_key_shared_by_two_hromadas_in_one_district_remains_unresolved() -> None:
    alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
        _record("300", "м. Кам’янське та Кам’янська територіальна громада", "Громада", 3),
        _record("303", "Перша територіальна громада", "Громада", 4),
        _record("304", "Друга територіальна громада", "Громада", 5),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12040150000015189", "Кам’янська", "H", "UA12040000000032213"),
        _term("UA12040250000000001", "Кам’янська", "H", "UA12040000000032213"),
        _term("UA12040150010000001", "Кам’янське", "M", "UA12040150000015189"),
        _term("UA12040250010000001", "Кам’янське", "M", "UA12040250000000001"),
        _term("UA12040030000000001", "Перша", "H", "UA12040000000032213"),
        _term("UA12040050000000001", "Друга", "H", "UA12040000000032213"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert not rows["300"].is_selectable
    assert rows["300"].external_code is None
    reasons = {item.uid: item.reason for item in result.unresolved}
    assert reasons["300"] == "community name is ambiguous inside provider district"


def test_exact_official_raion_renames_preserve_katottg_parentage() -> None:
    alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("16", "Луганська область", "Область", 4),
        _record("27", "Львівська область", "Область", 7),
        _record("22", "Харківська область", "Область", 10),
        _record("43", "Самарівський район", "Район", 2),
        _record("84", "Сіверськодонецький район", "Район", 5),
        _record("92", "Шептицький район", "Район", 8),
        _record("127", "Берестинський район", "Район", 11),
        _record("324", "Перша територіальна громада", "Громада", 3),
        _record("816", "Друга територіальна громада", "Громада", 6),
        _record("832", "Третя територіальна громада", "Громада", 9),
        _record("1324", "Четверта територіальна громада", "Громада", 12),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA44000000000018893", "Луганська", "O"),
        _term("UA46000000000026241", "Львівська", "O"),
        _term("UA63000000000041885", "Харківська", "O"),
        _term("UA12100000000031647", "Новомосковський", "P", "UA12000000000090473"),
        _term("UA44120000000092852", "Сєвєродонецький", "P", "UA44000000000018893"),
        _term("UA46120000000057093", "Червоноградський", "P", "UA46000000000026241"),
        _term("UA63060000000053698", "Красноградський", "P", "UA63000000000041885"),
        _term("UA12100010000078850", "Перша", "H", "UA12100000000031647"),
        _term("UA44120010000000001", "Друга", "H", "UA44120000000092852"),
        _term("UA46120010000000001", "Третя", "H", "UA46120000000057093"),
        _term("UA63060010000000001", "Четверта", "H", "UA63060000000053698"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["43"].external_code == "UA12100000000031647"
    assert rows["84"].external_code == "UA44120000000092852"
    assert rows["92"].external_code == "UA46120000000057093"
    assert rows["127"].external_code == "UA63060000000053698"
    assert rows["324"].parent_region_id == "43"
    assert rows["816"].parent_region_id == "84"
    assert rows["832"].parent_region_id == "92"
    assert rows["1324"].parent_region_id == "127"
    assert result.unresolved_uids == ()


def test_exact_hromada_kind_suffix_is_normalized_without_fuzzy_matching() -> None:
    alerts = _catalog(
        _record("28", "Донецька область", "Область", 1),
        _record("53", "Донецький район", "Район", 2),
        _record("403", "Амвросіївська міська громада", "Громада", 3),
    )
    payload = _payload(
        _term("UA14000000000091971", "Донецька", "O"),
        _term("UA14080000000022761", "Донецький", "P", "UA14000000000091971"),
        _term("UA14080010000058219", "Амвросіївська", "H", "UA14080000000022761"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["403"].external_code == "UA14080010000058219"
    assert rows["403"].parent_region_id == "53"
    assert result.unresolved_uids == ()


def test_exact_city_descendant_resolves_renamed_hromada_label() -> None:
    alerts = _catalog(
        _record("8", "Волинська область", "Область", 1),
        _record("40", "Володимирський район", "Район", 2),
        _record("255", "м. Володимир та Володимирська територіальна громада", "Громада", 3),
    )
    payload = _payload(
        _term("UA07000000000024379", "Волинська", "O"),
        _term("UA07020000000048211", "Володимирський", "P", "UA07000000000024379"),
        _term(
            "UA07020010000096841",
            "Володимир-Волинська",
            "H",
            "UA07020000000048211",
        ),
        _term(
            "UA07020010010041660",
            "Володимир",
            "M",
            "UA07020010000096841",
        ),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["255"].external_code == "UA07020010000096841"
    assert rows["255"].parent_region_id == "40"
    assert result.unresolved_uids == ()


def test_district_singleton_elimination_is_order_independent() -> None:
    alerts = _catalog(
        _record("2001", "м. Певне та Спільна територіальна громада", "Громада", 4),
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("2000", "Спільна територіальна громада", "Громада", 3),
        _record("42", "Кам'янський район", "Район", 2),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12040010000000001", "Спільна", "H", "UA12040000000032213"),
        _term("UA12040030000000001", "Спільна", "H", "UA12040000000032213"),
        _term("UA12040010010000001", "Певне", "M", "UA12040010000000001"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["2001"].external_code == "UA12040010000000001"
    assert rows["2000"].external_code == "UA12040030000000001"
    assert result.unresolved_uids == ()


def test_spelling_near_match_is_not_fuzzily_assigned() -> None:
    alerts = _catalog(
        _record("9", "Дніпропетровська область", "Область", 1),
        _record("42", "Кам'янський район", "Район", 2),
        _record("2000", "Камянська територіальна громада", "Громада", 3),
    )
    payload = _payload(
        _term("UA12000000000090473", "Дніпропетровська", "O"),
        _term("UA12040000000032213", "Кам’янський", "P", "UA12000000000090473"),
        _term("UA12040150000015189", "Кам’янська", "H", "UA12040000000032213"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    row = {item.region_id: item for item in result.rows}["2000"]
    assert not row.is_selectable
    assert row.external_code is None
    assert result.unresolved_uids == ("2000",)


def test_versioned_spelling_aliases_require_uid_name_parent_and_term_evidence() -> None:
    alerts = _catalog(
        _record("25", "Чернівецька область", "Область", 1),
        _record("137", "Чернівецький район", "Район", 2),
        _record("1515", "Веренчацька територіальна громада", "Громада", 3),
        _record("1525", "Карапачівська територіальна громада", "Громада", 4),
    )
    payload = _payload(
        _term("UA73000000000044923", "Чернівецька", "O"),
        _term("UA73060000000061247", "Чернівецький", "P", "UA73000000000044923"),
        _term("UA73060070000082869", "Веренчанська", "H", "UA73060000000061247"),
        _term("UA73060270000016588", "Карапчівська", "H", "UA73060000000061247"),
    )

    result = build_territory_catalog(payload, alerts_catalog=alerts)

    rows = {row.region_id: row for row in result.rows}
    assert rows["1515"].external_code == "UA73060070000082869"
    assert rows["1525"].external_code == "UA73060270000016588"
    assert len({row.external_code for row in result.rows}) == len(result.rows)
    assert result.unresolved_uids == ()
