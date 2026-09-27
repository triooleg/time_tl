"""Build a fail-closed alerts.in.ua hierarchy from official KATOTTG JSON."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from time_tl.infrastructure.db.territories import TerritoryDraftRow, validate_territory_rows

from .region_registry_full import FullRegionCatalog, LocationRecord, load_catalog

KATOTTG_AGENCY = "SSSU"
KATOTTG_CODELIST = "CL_KATOTTG"
KATOTTG_VERSION = "4.0.0"

_ALERT_KIND = {
    "Область": "OBLAST",
    "Місто з спеціальним статусом": "SPECIAL_STATUS_CITY",
    "Район": "RAION",
    "Громада": "HROMADA",
}
_KATOTTG_CATEGORY = {
    "OBLAST": "O",
    "SPECIAL_STATUS_CITY": "K",
    "RAION": "P",
    "HROMADA": "H",
}
_ROOT_KINDS = frozenset({"OBLAST", "SPECIAL_STATUS_CITY"})
_HIERARCHY_CATEGORIES = frozenset({"O", "K", "P", "H"})
_SETTLEMENT_CATEGORIES = frozenset({"M", "T", "C", "X"})
_PARSED_CATEGORIES = _HIERARCHY_CATEGORIES | _SETTLEMENT_CATEGORIES | {"B"}
_APOSTROPHE_TRANSLATION = str.maketrans({"\u2019": "'", "\u2018": "'"})

# Exact official district renames enacted after the pinned KATOTTG 4.0.0
# snapshot. These aliases never infer a hierarchy; the matched KATOTTG code
# remains the source of the parent relationship.
# https://zakon.rada.gov.ua/laws/show/3984-20
# https://zakon.rada.gov.ua/rada/show/v0281832-24
_OFFICIAL_RENAMED_RAION_ALIASES = {
    "берестинський": "красноградський",
    "самарівський": "новомосковський",
    "сіверськодонецький": "сєвєродонецький",
    "шептицький": "червоноградський",
}


class TerritoryCatalogBuildError(ValueError):
    """The supplied official source cannot be parsed without assumptions."""


@dataclass(frozen=True, slots=True)
class UnresolvedTerritory:
    uid: str
    name: str
    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class TerritoryCatalogBuildResult:
    rows: tuple[TerritoryDraftRow, ...]
    unresolved: tuple[UnresolvedTerritory, ...]
    alerts_source_sha256: str
    katottg_source_sha256: str
    source_sha256: str
    source_revision: str

    @property
    def unresolved_uids(self) -> tuple[str, ...]:
        return tuple(item.uid for item in self.unresolved)


@dataclass(frozen=True, slots=True)
class _KatottgTerm:
    code: str
    name: str
    category: str
    parent: str | None


@dataclass(frozen=True, slots=True)
class _VersionedCommunityAlias:
    provider_name: str
    parent_code: str
    katottg_code: str
    katottg_name: str


# Versioned, exact evidence exceptions for spelling drift between the pinned
# alerts.in.ua location export and SSSU:CL_KATOTTG(4.0.0). The provider UID,
# provider label, official parent and official H code/name must all match; this
# is deliberately not a fuzzy-name facility.
#
# alerts.in.ua source: region_registry_full.SOURCE_URL, pinned SHA-256
# a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620
# KATOTTG source: SSSU:CL_KATOTTG(4.0.0), term IDs below.
_VERSIONED_COMMUNITY_ALIASES = {
    # alerts UID 1515: "Веренчацька"; KATOTTG uses "Веренчанська".
    "1515": _VersionedCommunityAlias(
        "Веренчацька територіальна громада",
        "UA73060000000061247",
        "UA73060070000082869",
        "Веренчанська",
    ),
    # alerts UID 1525: "Карапачівська"; KATOTTG uses "Карапчівська".
    "1525": _VersionedCommunityAlias(
        "Карапачівська територіальна громада",
        "UA73060000000061247",
        "UA73060270000016588",
        "Карапчівська",
    ),  # Legal renames enacted by Law 3984-IX while KATOTTG 4.0.0 retains
    # the former community/city label.
    # https://zakon.rada.gov.ua/laws/show/3984-20
    "327": _VersionedCommunityAlias(
        "м. Самар та Самарівська територіальна громада",
        "UA12100000000039307",
        "UA12100070000011492",
        "Новомосковська",
    ),
    "315": _VersionedCommunityAlias(
        "м. Шахтарськ та Шахтарська територіальна громада",
        "UA12140000000011720",
        "UA12140210000089021",
        "Першотравенська",
    ),
    "832": _VersionedCommunityAlias(
        "м. Шептицький та Шептицька територіальна громада",
        "UA46120000000057093",
        "UA46120130000026331",
        "Червоноградська",
    ),
    "971": _VersionedCommunityAlias(
        "м. Південне та Південна територіальна громада",
        "UA51100000000095786",
        "UA51100410000059549",
        "Южненська",
    ),
    "1324": _VersionedCommunityAlias(
        "Берестинська територіальна громада",
        "UA63060000000053698",
        "UA63060050000033240",
        "Красноградська",
    ),
    "1299": _VersionedCommunityAlias(
        "м. Златопіль та Златопільська територіальна громада",
        "UA63100000000059237",
        "UA63100090000052019",
        "Первомайська",
    ),
    # Official Ukrainian spelling update; the parent and term ID are pinned.
    # https://zakon.rada.gov.ua/rada/show/v0281832-24
    "820": _VersionedCommunityAlias(
        "м. Сіверськодонецьк та Сіверськодонецька територіальна громада",
        "UA44120000000092852",
        "UA44120110000010163",
        "Сєвєродонецька",
    ),
    # Exact provider-export spelling/grammar drift. Each exception is bound to
    # one UID, one provider label, one P parent and one KATOTTG 4.0.0 H term.
    "407": _VersionedCommunityAlias(
        "Харцизьк міська громада",
        "UA14080000000098686",
        "UA14080090000014360",
        "Харцизька",
    ),
    "428": _VersionedCommunityAlias(
        "Райгородська територіальна громада",
        "UA18020000000072859",
        "UA18020110000026941",
        "Райгородоцька",
    ),
    "492": _VersionedCommunityAlias(
        "Дубриницько-Малоберезня територіальна громада",
        "UA21100000000055073",
        "UA21100070000018509",
        "Дубриницько-Малоберезнянська",
    ),
    "704": _VersionedCommunityAlias(
        "Гостомелська територіальна громада",
        "UA32080000000084076",
        "UA32080110000056605",
        "Гостомельська",
    ),
    "895": _VersionedCommunityAlias(
        "Новояворівськ територіальна громада",
        "UA46140000000036328",
        "UA46140050000054478",
        "Новояворівська",
    ),
    "1006": _VersionedCommunityAlias(
        "Криниченська територіальна громада",
        "UA51060000000057808",
        "UA51060110000077844",
        "Криничненська",
    ),
    "1044": _VersionedCommunityAlias(
        "Диканьська територіальна громада",
        "UA53080000000068029",
        "UA53080050000011754",
        "Диканська",
    ),
    "1433": _VersionedCommunityAlias(
        "Слобідсько-Кульчієвецьк територіальна громада",
        "UA68020000000016021",
        "UA68020230000069135",
        "Слобідсько-Кульчієвецька",
    ),
    "1466": _VersionedCommunityAlias(
        "Русько-Полянська територіальна громада",
        "UA71080000000036344",
        "UA71080350000033809",
        "Руськополянська",
    ),
}


def build_territory_catalog(
    katottg_json: bytes,
    *,
    alerts_catalog: FullRegionCatalog | None = None,
) -> TerritoryCatalogBuildResult:
    """Crosswalk pinned alerts IDs to caller-supplied official KATOTTG bytes."""

    catalog = alerts_catalog if alerts_catalog is not None else load_catalog()
    ordered_records = tuple(sorted(catalog.records, key=lambda record: record.source_row))
    terms, revision = _parse_katottg(katottg_json)
    term_by_code = {term.code: term for term in terms}
    by_category_name: dict[tuple[str, str], list[_KatottgTerm]] = defaultdict(list)
    for term in terms:
        if term.category not in _HIERARCHY_CATEGORIES:
            continue
        kind = _kind_for_category(term.category)
        by_category_name[(term.category, _normalize_name(term.name, kind))].append(term)
    settlement_hromadas_by_name: dict[tuple[str, str], set[str]] = defaultdict(set)
    for term in terms:
        if term.category not in _SETTLEMENT_CATEGORIES:
            continue
        ancestor = _hromada_ancestor(term, term_by_code)
        if ancestor is not None:
            settlement_hromadas_by_name[(_normalize_plain_name(term.name), term.category)].add(
                ancestor
            )

    rows_by_uid: dict[str, TerritoryDraftRow] = {}
    unresolved: dict[str, UnresolvedTerritory] = {}
    official_root_to_alert: dict[str, str] = {}
    district_code_by_alert: dict[str, str] = {}

    roots = [record for record in ordered_records if _kind(record) in _ROOT_KINDS]
    root_candidates: dict[str, tuple[_KatottgTerm, ...]] = {}
    candidate_root_uses: dict[str, list[str]] = defaultdict(list)
    for record in roots:
        kind = _kind(record)
        candidates = tuple(
            by_category_name.get(
                (_KATOTTG_CATEGORY[kind], _normalize_name(record.name_uk, kind)), []
            )
        )
        root_candidates[record.uid] = candidates
        if len(candidates) == 1:
            candidate_root_uses[candidates[0].code].append(record.uid)

    for record in roots:
        kind = _kind(record)
        candidates = root_candidates[record.uid]
        if len(candidates) == 1 and len(candidate_root_uses[candidates[0].code]) == 1:
            candidate = candidates[0]
            rows_by_uid[record.uid] = _draft(record, kind, None, candidate.code, True)
            official_root_to_alert[candidate.code] = record.uid
        else:
            reason = "missing official root" if not candidates else "ambiguous official root"
            rows_by_uid[record.uid] = _draft(record, kind, None, None, False)
            _mark_unresolved(unresolved, record, kind, reason)

    districts = [record for record in ordered_records if _kind(record) == "RAION"]
    district_candidates: dict[str, tuple[_KatottgTerm, ...]] = {}
    district_candidate_uses: dict[str, list[str]] = defaultdict(list)
    for record in districts:
        candidates = tuple(
            by_category_name.get(("P", _normalize_name(record.name_uk, "RAION")), [])
        )
        district_candidates[record.uid] = candidates
        if len(candidates) == 1:
            district_candidate_uses[candidates[0].code].append(record.uid)

    for record in districts:
        candidates = district_candidates[record.uid]
        unique_candidate = (
            candidates[0]
            if len(candidates) == 1 and len(district_candidate_uses[candidates[0].code]) == 1
            else None
        )
        if unique_candidate is not None:
            parent_uid = official_root_to_alert.get(unique_candidate.parent or "")
            official_parent = term_by_code.get(unique_candidate.parent or "")
            if (
                parent_uid is not None
                and official_parent is not None
                and official_parent.category == "O"
            ):
                rows_by_uid[record.uid] = _draft(
                    record, "RAION", parent_uid, unique_candidate.code, True
                )
                district_code_by_alert[record.uid] = unique_candidate.code
                continue

        parent_codes = {item.parent for item in candidates if item.parent}
        parent_uids = {
            official_root_to_alert[parent_code]
            for parent_code in parent_codes
            if parent_code in official_root_to_alert
        }
        if len(parent_uids) == 1:
            rows_by_uid[record.uid] = _draft(record, "RAION", next(iter(parent_uids)), None, False)
        reason = _district_failure_reason(candidates, unique_candidate)
        _mark_unresolved(unresolved, record, "RAION", reason)

    district_alert_by_code = {
        external_code: uid for uid, external_code in district_code_by_alert.items()
    }
    provider_district_by_community = _provider_district_contexts(ordered_records)
    communities_by_parent: dict[str, dict[str, tuple[LocationRecord, tuple[_KatottgTerm, ...]]]] = (
        defaultdict(dict)
    )

    for record in ordered_records:
        if _kind(record) != "HROMADA":
            continue
        provider_parent_uid = provider_district_by_community.get(record.uid)
        expected_parent_code = (
            district_code_by_alert.get(provider_parent_uid)
            if provider_parent_uid is not None
            else None
        )
        if provider_parent_uid is None or expected_parent_code is None:
            _mark_unresolved(
                unresolved, record, "HROMADA", "provider parent district is unresolved"
            )
            continue
        if district_alert_by_code.get(expected_parent_code) != provider_parent_uid:
            _mark_unresolved(
                unresolved, record, "HROMADA", "provider district context is inconsistent"
            )
            continue

        normalized_name = _normalize_name(record.name_uk, "HROMADA")
        community_candidates = [
            candidate
            for candidate in by_category_name.get(("H", normalized_name), [])
            if candidate.parent == expected_parent_code
        ]
        alias_candidate = _versioned_alias_candidate(record, expected_parent_code, term_by_code)
        if alias_candidate is not None:
            community_candidates = [alias_candidate]

        settlement_key = _community_settlement_key(record.name_uk)
        exact_city_hromadas = (
            settlement_hromadas_by_name.get((settlement_key, "M"), set())
            if settlement_key is not None
            else set()
        )
        exact_city_candidates = [
            term_by_code[code]
            for code in exact_city_hromadas
            if code in term_by_code
            and term_by_code[code].category == "H"
            and term_by_code[code].parent == expected_parent_code
        ]
        if not community_candidates and exact_city_candidates:
            # ``м. X та ... громада`` gives an exact official city name. The
            # M -> H ancestor is accepted only inside the preceding provider
            # district; same-named settlements elsewhere cannot leak in.
            community_candidates = exact_city_candidates
        elif len(community_candidates) > 1 and exact_city_candidates:
            exact_city_codes = {candidate.code for candidate in exact_city_candidates}
            narrowed = [
                candidate
                for candidate in community_candidates
                if candidate.code in exact_city_codes
            ]
            if narrowed:
                community_candidates = narrowed

        deduplicated = {candidate.code: candidate for candidate in community_candidates}
        communities_by_parent[provider_parent_uid][record.uid] = (
            record,
            tuple(deduplicated[code] for code in sorted(deduplicated)),
        )

    for parent_uid, community_entries in communities_by_parent.items():
        candidate_codes = {
            uid: tuple(candidate.code for candidate in candidates)
            for uid, (_, candidates) in community_entries.items()
        }
        resolved_codes = _resolve_district_candidates(candidate_codes)
        for uid, (record, candidates) in community_entries.items():
            external_code = resolved_codes.get(uid)
            if external_code is not None:
                rows_by_uid[uid] = _draft(record, "HROMADA", parent_uid, external_code, True)
                continue

            rows_by_uid[uid] = _draft(record, "HROMADA", parent_uid, None, False)
            if not candidates:
                reason = "missing official community in provider district"
            elif len(candidates) > 1:
                reason = "community name is ambiguous inside provider district"
            else:
                reason = "district-scoped exact mapping is not unique"
            _mark_unresolved(unresolved, record, "HROMADA", reason)
    for uid, row in tuple(rows_by_uid.items()):
        if row.external_code is None or row.kind not in {"RAION", "HROMADA"}:
            continue
        official = term_by_code[row.external_code]
        expected_parent_category = "O" if row.kind == "RAION" else "P"
        official_parent = term_by_code.get(official.parent or "")
        if official_parent is None or official_parent.category != expected_parent_category:
            record = catalog.by_uid[uid]
            rows_by_uid[uid] = _draft(record, row.kind, row.parent_region_id, None, False)
            _mark_unresolved(unresolved, record, row.kind, "invalid official parent type")

    child_kinds: dict[str, set[str]] = defaultdict(set)
    for row in rows_by_uid.values():
        if row.parent_region_id is not None:
            child_kinds[row.parent_region_id].add(row.kind)
    for uid, row in tuple(rows_by_uid.items()):
        if row.kind == "RAION" and "HROMADA" not in child_kinds.get(uid, set()):
            del rows_by_uid[uid]
            record = catalog.by_uid[uid]
            _mark_unresolved(unresolved, record, row.kind, "no safely resolved community")

    rows = tuple(rows_by_uid[record.uid] for record in ordered_records if record.uid in rows_by_uid)
    validate_territory_rows(rows)
    alerts_hash = catalog.sha256.lower()
    katottg_hash = hashlib.sha256(katottg_json).hexdigest()
    combined_hash = hashlib.sha256(f"{alerts_hash}:{katottg_hash}".encode()).hexdigest()
    return TerritoryCatalogBuildResult(
        rows=rows,
        unresolved=tuple(
            unresolved[record.uid] for record in ordered_records if record.uid in unresolved
        ),
        alerts_source_sha256=alerts_hash,
        katottg_source_sha256=katottg_hash,
        source_sha256=combined_hash,
        source_revision=revision,
    )


def _provider_district_contexts(
    records: Sequence[LocationRecord],
) -> dict[str, str]:
    """Return H UID -> nearest preceding provider P UID by pinned source row."""

    result: dict[str, str] = {}
    current_district_uid: str | None = None
    for record in sorted(records, key=lambda item: item.source_row):
        kind = _kind(record)
        if kind in _ROOT_KINDS:
            current_district_uid = None
        elif kind == "RAION":
            current_district_uid = record.uid
        elif kind == "HROMADA" and current_district_uid is not None:
            result[record.uid] = current_district_uid
    return result


def _versioned_alias_candidate(
    record: LocationRecord,
    expected_parent_code: str,
    terms_by_code: Mapping[str, _KatottgTerm],
) -> _KatottgTerm | None:
    alias = _VERSIONED_COMMUNITY_ALIASES.get(record.uid)
    if alias is None:
        return None
    if _normalize_plain_name(record.name_uk) != _normalize_plain_name(alias.provider_name):
        # A caller-supplied fixture may reuse a production UID. Aliases are
        # activated only by the complete UID + provider-name evidence tuple.
        return None
    if expected_parent_code != alias.parent_code:
        raise TerritoryCatalogBuildError(
            f"versioned alias parent mismatch for alerts UID {record.uid!r}"
        )
    candidate = terms_by_code.get(alias.katottg_code)
    if (
        candidate is None
        or candidate.category != "H"
        or candidate.parent != alias.parent_code
        or _normalize_plain_name(candidate.name) != _normalize_plain_name(alias.katottg_name)
    ):
        raise TerritoryCatalogBuildError(
            f"versioned KATOTTG evidence mismatch for alerts UID {record.uid!r}"
        )
    return candidate


def _resolve_district_candidates(
    candidates_by_uid: Mapping[str, tuple[str, ...]],
) -> dict[str, str]:
    """Resolve only forced exact assignments inside one provider district.

    Iterative singleton elimination handles cases such as A->{x,y}, B->{x}
    without relying on provider order. Duplicate singletons and cycles remain
    unresolved because they admit no unique, evidence-backed assignment.
    """

    pending = {uid: set(codes) for uid, codes in candidates_by_uid.items()}
    resolved: dict[str, str] = {}
    used_codes: set[str] = set()
    while True:
        for codes in pending.values():
            codes.difference_update(used_codes)
        singleton_codes: dict[str, list[str]] = defaultdict(list)
        for uid, codes in pending.items():
            if uid not in resolved and len(codes) == 1:
                singleton_codes[next(iter(codes))].append(uid)
        forced = {uids[0]: code for code, uids in singleton_codes.items() if len(uids) == 1}
        if not forced:
            return resolved
        resolved.update(forced)
        used_codes.update(forced.values())


def _parse_katottg(payload: bytes) -> tuple[tuple[_KatottgTerm, ...], str]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerritoryCatalogBuildError("KATOTTG payload is not valid JSON") from error
    if not isinstance(document, Mapping):
        raise TerritoryCatalogBuildError("KATOTTG JSON root must be an object")

    glossary: Mapping[str, Any] = document
    data = document.get("data")
    if isinstance(data, Mapping):
        raw_glossaries = data.get("glossaries")
        if not isinstance(raw_glossaries, Sequence) or isinstance(raw_glossaries, (str, bytes)):
            raise TerritoryCatalogBuildError("KATOTTG response has no data.glossaries array")
        matches = [
            item
            for item in raw_glossaries
            if isinstance(item, Mapping)
            and item.get("id") == KATOTTG_CODELIST
            and item.get("version") == KATOTTG_VERSION
        ]
        if len(matches) != 1:
            raise TerritoryCatalogBuildError("KATOTTG response must contain one requested glossary")
        glossary = matches[0]

    codelist_id = _string_field(glossary, "id")
    version = _string_field(glossary, "version")
    agency = _optional_string_field(glossary, "agency", "agencyId", "agencyID") or KATOTTG_AGENCY
    if codelist_id != KATOTTG_CODELIST or version != KATOTTG_VERSION or agency != KATOTTG_AGENCY:
        raise TerritoryCatalogBuildError("expected official SSSU:CL_KATOTTG(4.0.0) glossary")
    raw_terms = glossary.get("terms")
    if not isinstance(raw_terms, Sequence) or isinstance(raw_terms, (str, bytes)):
        raise TerritoryCatalogBuildError("KATOTTG glossary has no terms array")

    parsed: list[_KatottgTerm] = []
    seen: set[str] = set()
    for raw in raw_terms:
        if not isinstance(raw, Mapping):
            raise TerritoryCatalogBuildError("KATOTTG term must be an object")
        category = _attribute(raw.get("attributes"), "KATOTTG_Category")
        if category not in _PARSED_CATEGORIES:
            continue
        code = _string_field(raw, "id")
        if code in seen:
            raise TerritoryCatalogBuildError(f"duplicate KATOTTG term {code!r}")
        seen.add(code)
        name = _localized_uk_name(raw.get("names"))
        parent = _optional_string_field(raw, "parent")
        if category not in {"O", "K"} and parent is None:
            raise TerritoryCatalogBuildError(f"KATOTTG term {code!r} is missing parent")
        parsed.append(_KatottgTerm(code, name, category, parent))
    if not parsed:
        raise TerritoryCatalogBuildError("KATOTTG glossary has no O/K/P/H terms")
    return tuple(parsed), f"{agency}:{codelist_id}({version})"


def _localized_uk_name(raw: object) -> str:
    if isinstance(raw, Mapping):
        value = raw.get("uk")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = value.get("value") or value.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            locale = item.get("locale") or item.get("lang")
            value = item.get("value") or item.get("name")
            if locale == "uk" and isinstance(value, str) and value.strip():
                return value.strip()
    raise TerritoryCatalogBuildError("KATOTTG term has no Ukrainian name")


def _attribute(raw: object, key: str) -> str | None:
    if isinstance(raw, Mapping):
        return _scalar(raw.get(key))
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            if item.get("id") == key or item.get("name") == key:
                return _scalar(item.get("value"))
    return None


def _scalar(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, Mapping):
        value = raw.get("value") or raw.get("id")
        if isinstance(value, str):
            return value.strip() or None
    return None


def _string_field(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TerritoryCatalogBuildError(f"KATOTTG field {key!r} is missing")
    return value.strip()


def _optional_string_field(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_name(name: str, kind: str) -> str:
    value = _normalize_plain_name(name)
    if kind == "SPECIAL_STATUS_CITY":
        value = re.sub(r"^м\.\s*", "", value)
    elif kind == "OBLAST":
        value = re.sub(r"\s+область$", "", value)
    elif kind == "RAION":
        value = re.sub(r"\s+район$", "", value)
        value = _OFFICIAL_RENAMED_RAION_ALIASES.get(value, value)
    elif kind == "HROMADA":
        if value.startswith("м. ") and " та " in value:
            value = value.split(" та ", 1)[1]
        value = re.sub(
            r"\s+(?:(?:територіальна|міська|селищна|сільська)\s+)?громада$",
            "",
            value,
        )
    return value.strip()


def _normalize_plain_name(name: str) -> str:
    value = unicodedata.normalize("NFC", name).translate(_APOSTROPHE_TRANSLATION).casefold()
    return re.sub(r"\s+", " ", value).strip()


def _community_settlement_key(name: str) -> str | None:
    value = _normalize_plain_name(name)
    match = re.fullmatch(
        r"м\.\s*(?P<settlement>.+?)\s+та\s+.+?\s+територіальна громада",
        value,
    )
    if match is None:
        return None
    return match.group("settlement").strip()


def _hromada_ancestor(term: _KatottgTerm, terms_by_code: Mapping[str, _KatottgTerm]) -> str | None:
    current = term
    visited = {term.code}
    while current.parent is not None:
        parent = terms_by_code.get(current.parent)
        if parent is None or parent.code in visited:
            return None
        if parent.category == "H":
            return parent.code
        visited.add(parent.code)
        current = parent
    return None


def _kind(record: LocationRecord) -> str:
    try:
        return _ALERT_KIND[record.location_type]
    except KeyError as error:
        raise TerritoryCatalogBuildError(
            f"alerts UID {record.uid!r} has unsupported type {record.location_type!r}"
        ) from error


def _kind_for_category(category: str) -> str:
    for kind, candidate in _KATOTTG_CATEGORY.items():
        if candidate == category:
            return kind
    raise TerritoryCatalogBuildError(f"unsupported KATOTTG category {category!r}")


def _draft(
    record: LocationRecord,
    kind: str,
    parent_uid: str | None,
    external_code: str | None,
    selectable: bool,
) -> TerritoryDraftRow:
    return TerritoryDraftRow(
        region_id=record.uid,
        name=record.name_uk,
        kind=kind,
        parent_region_id=parent_uid,
        external_code=external_code,
        sort_key=_normalize_name(record.name_uk, kind),
        is_selectable=selectable,
    )


def _mark_unresolved(
    target: dict[str, UnresolvedTerritory],
    record: LocationRecord,
    kind: str,
    reason: str,
) -> None:
    target[record.uid] = UnresolvedTerritory(record.uid, record.name_uk, kind, reason)


def _district_failure_reason(
    candidates: tuple[_KatottgTerm, ...], unique_candidate: _KatottgTerm | None
) -> str:
    if not candidates:
        return "missing official district"
    if unique_candidate is not None:
        return "official parent oblast is unresolved"
    if len({candidate.parent for candidate in candidates}) > 1:
        return "district name is ambiguous across oblasts"
    return "district mapping is ambiguous"
