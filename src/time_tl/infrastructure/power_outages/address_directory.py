"""Read-only documented address candidates with transient issued-choice chaining."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any
from weakref import WeakValueDictionary

from time_tl.domain.power_outages.address import (
    AssignmentChoice,
    HouseChoice,
    SettlementChoice,
    StreetChoice,
    katottg,
    public_text,
)
from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import PowerProvider, validate_group

from .alerts_energy_api import AlertsEnergyApiClient
from .alerts_energy_api_normalizer import KNOWN_BINDINGS, parse_api_json
from .base import PowerFetchError, PowerFetchErrorCode

Choice = SettlementChoice | StreetChoice | HouseChoice | AssignmentChoice
SUPPORTED_AREAS = frozenset(area for area, supplier in KNOWN_BINDINGS.values())


def _list(value: object, limit: int) -> list[Any]:
    if type(value) is not list or len(value) > limit:
        raise ValueError("invalid_power_address_response")
    return value


def _dedupe[T: Choice](values: Sequence[T], key: Callable[[T], str]) -> tuple[T, ...]:
    found: dict[str, T] = {}
    for value in values:
        identifier = key(value)
        if identifier in found and found[identifier] != value:
            raise ValueError("ambiguous_power_address_choice")
        found[identifier] = value
    return tuple(found.values())


def _query(value: str, maximum: int) -> str:
    public_text(value, maximum=maximum)
    if len(value.strip()) < 2 or value != value.strip():
        raise ValueError("invalid_power_address_query")
    return value


def _settlement(row: object, seed: PowerLocationSeed) -> SettlementChoice | None:
    if type(row) is not dict or set(row) != {"settlement", "ancestors"}:
        raise ValueError("invalid_power_settlement_response")
    node, ancestors = row["settlement"], _list(row["ancestors"], 5)
    nodes = [node, *ancestors]
    registry: dict[str, dict[str, Any]] = {}
    for item in nodes:
        if type(item) is not dict or not {
            "katottg", "parent_katottg", "name", "slug", "category", "level",
        } <= set(item):
            raise ValueError("invalid_power_settlement_node")
        code = katottg(item["katottg"])
        if code in registry or type(item["level"]) is not int or not 1 <= item["level"] <= 4:
            raise ValueError("ambiguous_power_settlement_chain")
        public_text(item["name"])
        public_text(item["slug"], maximum=128)
        if item["parent_katottg"] is not None:
            katottg(item["parent_katottg"])
        registry[code] = item
    if (type(node) is not dict or node["category"] not in {"M", "C", "X"}
            or node["level"] != 4):
        return None
    categories = {1: {"O"}, 2: {"P"}, 3: {"H"}, 4: {"M", "C", "X"}}
    if any(item["category"] not in categories[item["level"]] for item in nodes):
        return None
    chain: list[dict[str, Any]] = []
    current = node
    seen = set()
    while True:
        code = current["katottg"]
        if code in seen:
            raise ValueError("cyclic_power_settlement_chain")
        chain.append(current)
        seen.add(code)
        parent = current["parent_katottg"]
        if parent is None:
            break
        if parent not in registry or registry[parent]["level"] != current["level"] - 1:
            raise ValueError("incomplete_power_settlement_chain")
        current = registry[parent]
    if len(seen) != len(nodes) or len(chain) != 4:
        raise ValueError("unrelated_power_settlement_ancestor")
    seed_level = {"OBLAST": 1, "RAION": 2, "HROMADA": 3, "SPECIAL_STATUS_CITY": 1}
    matched = next((item for item in chain if item["katottg"] == seed.katottg), None)
    if matched is None or matched["level"] != seed_level[seed.territory_kind]:
        return None
    root = chain[-1]
    if (root["level"] != 1 or root["category"] != "O"
            or root["slug"] not in SUPPORTED_AREAS):
        return None
    return SettlementChoice(node["katottg"], node["name"], seed.katottg,
                            seed.catalog_version, root["katottg"], root["slug"])


class AlertsEnergyAddressDirectory:
    def __init__(self, client: AlertsEnergyApiClient) -> None:
        if not isinstance(client, AlertsEnergyApiClient):
            raise ValueError("invalid_power_address_client")
        self._client = client
        self._issued: WeakValueDictionary[int, Choice] = WeakValueDictionary()

    def _remember[T: Choice](self, values: tuple[T, ...]) -> tuple[T, ...]:
        if len(self._issued) + len(values) > 4096:
            raise ValueError("power_address_session_capacity")
        for value in values:
            self._issued[id(value)] = value
        return values

    def _require(self, value: Choice) -> None:
        if self._issued.get(id(value)) is not value:
            raise ValueError("power_address_choice_expired")

    def _scope(self, seed: PowerLocationSeed, settlement: SettlementChoice) -> None:
        self._require(settlement)
        if seed.katottg != settlement.seed_katottg \
                or seed.catalog_version != settlement.catalog_version:
            raise ValueError("power_address_territory_changed")

    async def settlements(self, seed: PowerLocationSeed, q: str = "") \
            -> tuple[SettlementChoice, ...]:
        if type(seed) is not PowerLocationSeed:
            raise ValueError("invalid_power_address_seed")
        if not seed.needs_settlement:
            # Exact special-city geography can be reused, never a guessed operator.
            return self._remember((SettlementChoice(
                seed.katottg, seed.label, seed.katottg, seed.catalog_version, seed.katottg,
            ),))
        query = _query(q, 20)
        value = await self._client._get("atus/settlement-search", params={"q": query})
        try:
            choices = []
            for row in _list(value, 100):
                try:
                    choice = _settlement(row, seed)
                except (ValueError, TypeError, KeyError):
                    # A malformed/unrelated hierarchy cannot be selected.
                    continue
                if choice is not None:
                    choices.append(choice)
            return self._remember(_dedupe(choices, lambda item: item.katottg))
        except (ValueError, TypeError, KeyError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def streets(self, seed: PowerLocationSeed, settlement: SettlementChoice, q: str) \
            -> tuple[StreetChoice, ...]:
        self._scope(seed, settlement)
        query = _query(q, 80)
        if settlement.area_slug not in SUPPORTED_AREAS:
            return ()
        value = await self._client._get("address-directory/street-search", params={
            "katottg": settlement.katottg, "q": query,
        })
        try:
            choices = []
            for row in _list(value, 20):
                if type(row) is not dict or set(row) != {"value", "label"}:
                    raise ValueError("invalid_power_street_response")
                choices.append(StreetChoice(row["value"], row["label"], settlement))
            return self._remember(_dedupe(choices, lambda item: item.street_id))
        except (ValueError, TypeError, KeyError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def houses(self, street: StreetChoice) -> tuple[HouseChoice, ...]:
        self._require(street)
        value = await self._client._get(f"address-directory/streets/{street.street_id}/houses")
        try:
            choices = []
            for row in _list(value, 512):
                if type(row) is not dict or set(row) != {"value", "label"}:
                    raise ValueError("invalid_power_house_response")
                pair = parse_api_json(public_text(row["value"], maximum=512).encode("utf-8"))
                if type(pair) is not list or len(pair) != 2:
                    raise ValueError("invalid_power_house_pair")
                canonical = json.dumps(pair, ensure_ascii=False, separators=(",", ":"))
                choices.append(HouseChoice(canonical, pair[0], pair[1], row["label"], street))
            # Deduplicate canonical number/building even if JSON spacing differs.
            return self._remember(_dedupe(
                choices, lambda item: item.number + "\x00" + item.building,
            ))
        except (ValueError, TypeError, KeyError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def assignments(self, seed: PowerLocationSeed, settlement: SettlementChoice,
                          street: StreetChoice, house: HouseChoice) \
            -> tuple[AssignmentChoice, ...]:
        self._scope(seed, settlement)
        self._require(street)
        self._require(house)
        if street.settlement is not settlement or house.street is not street:
            raise ValueError("power_address_selection_mismatch")
        if settlement.area_slug not in SUPPORTED_AREAS:
            return ()
        suppliers = await self._client._get(
            f"source-registry/areas/{settlement.area_slug}/suppliers",
        )
        try:
            supplier_map: dict[str, PowerProvider] = {}
            for row in _list(suppliers, 128):
                if type(row) is not dict or row.get("area_slug") != settlement.area_slug:
                    raise ValueError("invalid_power_address_suppliers")
                for provider, binding in KNOWN_BINDINGS.items():
                    if binding == (settlement.area_slug, row.get("supplier_key")):
                        slug = public_text(row.get("slug"), maximum=128)
                        if slug in supplier_map:
                            raise ValueError("ambiguous_power_address_supplier")
                        supplier_map[slug] = provider
            if not supplier_map:
                return ()
        except (ValueError, TypeError, KeyError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None
        value = await self._client._get(
            f"address-directory/streets/{street.street_id}/queues",
            params={"house": house.number, "building": house.building},
        )
        try:
            if type(value) is not dict or set(value) != {"assignments"}:
                raise ValueError("invalid_power_address_assignments")
            choices = []
            for row in _list(value["assignments"], 64):
                if type(row) is not dict or set(row) != {
                    "id", "supplier_slug", "supplier_name", "queue",
                }:
                    raise ValueError("invalid_power_address_assignment")
                matched_provider = supplier_map.get(row["supplier_slug"])
                if matched_provider is None:
                    continue
                group = validate_group(row["queue"])
                choices.append(AssignmentChoice(row["id"], matched_provider, group,
                                                f"{matched_provider.label} · {group}"))
            return self._remember(_dedupe(choices, lambda item: item.assignment_id))
        except (ValueError, TypeError, KeyError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None
