"""SYNTHETIC scoped-address records; no live addresses, keys or API calls."""

import logging
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from pydantic import SecretStr

from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.power_outages.address_directory import AlertsEnergyAddressDirectory
from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode

ROOT, RAION, COMMUNITY, CITY, OTHER = ("UA" + str(n).zfill(17) for n in range(1, 6))
STREET = "a" * 24
KEY = "SYNTHETIC-private-key"
AREA = "dnipropetrovska-oblast"
SUPPLIER = "dnipropetrovska_oblast_oblenergo"
SEED = PowerLocationSeed(COMMUNITY, "HROMADA", "Synthetic community", "synthetic-v1")


def territory(code, parent, level, category, slug="synthetic-node"):
    return {"katottg": code, "parent_katottg": parent, "level": level,
            "category": category, "slug": slug, "name": "SYNTHETIC-PRIVATE-NAME"}


def settlement_rows():
    return [{"settlement": territory(CITY, COMMUNITY, 4, "M"), "ancestors": [
        territory(COMMUNITY, RAION, 3, "H"), territory(ROOT, None, 1, "O", AREA),
        territory(RAION, ROOT, 2, "P"),
    ]}]


class DirectoryFixture:
    def __init__(self):
        self.calls = []
        self.settlements = settlement_rows()
        self.streets = [{"value": STREET, "label": "SYNTHETIC-PRIVATE-STREET"}]
        self.houses = [{"value": '["12","B"]', "label": "SYNTHETIC-PRIVATE-HOUSE"}]
        self.suppliers = [{"supplier_key": SUPPLIER, "area_slug": AREA,
                           "slug": "synthetic-dtek-slug"}]
        self.assignments = {"assignments": [{"id": STREET + ":0",
            "supplier_slug": "synthetic-dtek-slug", "supplier_name": "Synthetic DTEK",
            "queue": "1.1"}]}
        self.failure = None
        self.client = AlertsEnergyApiClient(
            api_key=SecretStr(KEY), transport=httpx.MockTransport(self.handle),
        )
        self.directory = AlertsEnergyAddressDirectory(self.client)

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "alerts.energy" and request.url.scheme == "https"
        if self.failure:
            return httpx.Response(self.failure, headers={"Retry-After": "300"},
                                  text="PRIVATE-ADDRESS " + KEY)
        path = request.url.path
        if path.endswith("/settlement-search"):
            return httpx.Response(200, json=self.settlements)
        if path.endswith("/street-search"):
            return httpx.Response(200, json=self.streets)
        if path.endswith("/houses"):
            return httpx.Response(200, json=self.houses)
        if path.endswith("/suppliers"):
            return httpx.Response(200, json=self.suppliers)
        if path.endswith("/queues"):
            return httpx.Response(200, json=self.assignments)
        raise AssertionError("undocumented route")

    async def chain(self):
        settlement, = await self.directory.settlements(SEED, "Синтетичне")
        street, = await self.directory.streets(SEED, settlement, "Синтетична")
        house, = await self.directory.houses(street)
        return settlement, street, house


async def test_full_chain_known_provider_and_group_candidate_only():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    result, = await fixture.directory.assignments(SEED, settlement, street, house)
    assert result.provider is PowerProvider.DTEK and result.group_id == "1.1"
    assert settlement.seed_katottg == COMMUNITY and settlement.area_katottg == ROOT
    assert settlement.area_slug == AREA and street.settlement is settlement
    assert house.street is street and house.number == "12" and house.building == "B"
    assert fixture.calls[-1].url.params["house"] == "12"
    assert fixture.calls[-1].url.params["building"] == "B"
    assert fixture.calls[1].url.params["katottg"] == CITY
    assert SEED.katottg == COMMUNITY


async def test_ancestor_order_irrelevant_but_entire_link_chain_required():
    fixture = DirectoryFixture()
    fixture.settlements[0]["ancestors"].reverse()
    choices = await fixture.directory.settlements(SEED, "Синтетичне")
    assert len(choices) == 1


@pytest.mark.parametrize("change", ["missing", "cycle", "unrelated", "wrong_seed",
                                    "wrong_level", "unknown_area", "not_settlement"])
async def test_invalid_or_other_community_never_selected(change):
    fixture = DirectoryFixture()
    row = fixture.settlements[0]
    if change == "missing":
        row["ancestors"].pop()
    elif change == "cycle":
        row["ancestors"][1]["parent_katottg"] = CITY
    elif change == "unrelated":
        row["ancestors"].append(territory(OTHER, None, 1, "O"))
    elif change == "wrong_seed":
        row["settlement"]["parent_katottg"] = OTHER
        row["ancestors"][0]["katottg"] = OTHER
    elif change == "wrong_level":
        row["ancestors"][0]["level"] = 2
    elif change == "unknown_area":
        row["ancestors"][1]["slug"] = "unknown-area"
    else:
        row["settlement"]["category"] = "H"
    assert await fixture.directory.settlements(SEED, "Синтетичне") == ()


async def test_special_city_reuses_exact_seed_without_query_or_operator_guess():
    fixture = DirectoryFixture()
    seed = PowerLocationSeed(CITY, "SPECIAL_STATUS_CITY", "Synthetic city", "v1")
    settlement, = await fixture.directory.settlements(seed)
    assert settlement.katottg == CITY and settlement.name == seed.label
    assert fixture.calls == []
    assert await fixture.directory.streets(seed, settlement, "Вулиця") == ()
    assert fixture.calls == []


@pytest.mark.parametrize("q", ["", "x", " x", "x ", "a" * 21, "x\ny"])
async def test_settlement_query_bounds_reject_before_http(q):
    fixture = DirectoryFixture()
    with pytest.raises(ValueError):
        await fixture.directory.settlements(SEED, q)
    assert fixture.calls == []


async def test_street_query_encoded_once_cannot_inject_host_or_other_parameters():
    fixture = DirectoryFixture()
    settlement, = await fixture.directory.settlements(SEED, "Синтетичне")
    query = "Вул &house=evil?next=https://evil.invalid/"
    await fixture.directory.streets(SEED, settlement, query)
    request = fixture.calls[-1]
    assert request.url.params["q"] == query
    assert set(request.url.params) == {"katottg", "q"}


@pytest.mark.parametrize("which", ["settlement_copy", "street_copy", "house_copy",
                                   "changed_seed", "new_directory"])
async def test_forged_replayed_or_changed_scope_choices_rejected_before_http(which):
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    before = len(fixture.calls)
    if which == "settlement_copy":
        settlement = replace(settlement)
    elif which == "street_copy":
        street = replace(street)
    elif which == "house_copy":
        house = replace(house)
    elif which == "new_directory":
        fixture.directory = AlertsEnergyAddressDirectory(fixture.client)
    seed = replace(SEED, catalog_version="new") if which == "changed_seed" else SEED
    with pytest.raises(ValueError):
        await fixture.directory.assignments(seed, settlement, street, house)
    assert len(fixture.calls) == before


async def test_different_issued_street_cannot_be_combined_with_house():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.streets = [{"value": "b" * 24, "label": "Other synthetic street"}]
    other, = await fixture.directory.streets(SEED, settlement, "Вулиця")
    before = len(fixture.calls)
    with pytest.raises(ValueError, match="mismatch"):
        await fixture.directory.assignments(SEED, settlement, other, house)
    assert len(fixture.calls) == before


@pytest.mark.parametrize("bad_id", ["../evil", "a" * 23, "a" * 25, "?house=1", "g" * 24])
async def test_street_path_identifiers_are_strict_returned_24hex(bad_id):
    fixture = DirectoryFixture()
    settlement, = await fixture.directory.settlements(SEED, "Синтетичне")
    fixture.streets[0]["value"] = bad_id
    with pytest.raises(PowerFetchError):
        await fixture.directory.streets(SEED, settlement, "Вулиця")


@pytest.mark.parametrize("pair", ['[]', '["1"]', '["1","",3]', '[1,""]',
                                 '["1",null]', 'null', '["1","x\\ny"]'])
async def test_invalid_house_value_pair_rejected(pair):
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.houses[0]["value"] = pair
    with pytest.raises(PowerFetchError):
        await fixture.directory.houses(street)


async def test_duplicate_choices_collapse_without_multiple_subscription_candidates():
    fixture = DirectoryFixture()
    fixture.settlements *= 2
    settlement, = await fixture.directory.settlements(SEED, "Синтетичне")
    fixture.streets *= 2
    street, = await fixture.directory.streets(SEED, settlement, "Вулиця")
    fixture.houses += [{"value": '[ "12", "B" ]', "label": "SYNTHETIC-PRIVATE-HOUSE"}]
    house, = await fixture.directory.houses(street)
    fixture.assignments["assignments"] *= 2
    choices = await fixture.directory.assignments(SEED, settlement, street, house)
    assert len(choices) == 1


async def test_conflicting_duplicate_street_not_silently_first_wins():
    fixture = DirectoryFixture()
    settlement, = await fixture.directory.settlements(SEED, "Синтетичне")
    fixture.streets.append({"value": STREET, "label": "Contradictory street"})
    with pytest.raises(PowerFetchError):
        await fixture.directory.streets(SEED, settlement, "Вулиця")


@pytest.mark.parametrize("status", [404, 403, 429, 500])
async def test_http_failure_is_unknown_not_no_outages_and_no_retry(status):
    fixture = DirectoryFixture()
    fixture.failure = status
    with pytest.raises(PowerFetchError) as caught:
        await fixture.directory.settlements(SEED, "Синтетичне")
    assert caught.value.status_code == status and len(fixture.calls) == 1
    assert KEY not in str(caught.value) and "PRIVATE-ADDRESS" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_after_seconds == 300


async def test_unknown_operator_ignored_not_relabelled_as_dtek():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.assignments["assignments"][0]["supplier_slug"] = "unknown-operator"
    assert await fixture.directory.assignments(SEED, settlement, street, house) == ()


async def test_supplier_area_mismatch_cannot_produce_candidate():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.suppliers[0]["area_slug"] = "other-region"
    with pytest.raises(PowerFetchError):
        await fixture.directory.assignments(SEED, settlement, street, house)


async def test_unknown_queue_never_guessed_or_offset_mapped():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.assignments["assignments"][0]["queue"] = "5001.1"
    with pytest.raises(PowerFetchError):
        await fixture.directory.assignments(SEED, settlement, street, house)


async def test_cek_candidate_uses_actual_api_supplier_slug_mapping():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    fixture.suppliers[0].update(supplier_key="dnipropetrovska_oblast_cek",
                                slug="synthetic-cek-different-from-key")
    fixture.assignments["assignments"][0]["supplier_slug"] = "synthetic-cek-different-from-key"
    result, = await fixture.directory.assignments(SEED, settlement, street, house)
    assert result.provider is PowerProvider.CEK


async def test_empty_directory_is_empty_candidates_not_power_state():
    fixture = DirectoryFixture()
    fixture.settlements = []
    assert await fixture.directory.settlements(SEED, "Синтетичне") == ()


async def test_choices_are_immutable_and_repr_does_not_reveal_addresses():
    fixture = DirectoryFixture()
    settlement, street, house = await fixture.chain()
    assignment, = await fixture.directory.assignments(SEED, settlement, street, house)
    for choice in (settlement, street, house, assignment):
        assert "SYNTHETIC-PRIVATE" not in repr(choice)
        assert STREET not in repr(choice) and COMMUNITY not in repr(choice)
    with pytest.raises(FrozenInstanceError):
        street.label = "changed"


async def test_private_queries_do_not_appear_in_httpx_info_logs(caplog):
    fixture = DirectoryFixture()
    caplog.set_level(logging.INFO, logger="httpx")
    settlement, street, house = await fixture.chain()
    await fixture.directory.assignments(SEED, settlement, street, house)
    assert not [record for record in caplog.records if record.name == "httpx" and any(
        path in record.getMessage() for path in ("/address-directory/", "/atus/settlement-search")
    )]
    # A normal API check retains unrelated diagnostic logging.
    fixture.client._transport = httpx.MockTransport(lambda request: httpx.Response(200,
                                                                                 json={"ok": True}))
    await fixture.client.check_access()
    assert any("/api/v1/check" in record.getMessage() for record in caplog.records)
    assert KEY not in caplog.text and "Синтетична" not in caplog.text


async def test_oversized_response_choices_fail_closed():
    fixture = DirectoryFixture()
    settlement, = await fixture.directory.settlements(SEED, "Синтетичне")
    fixture.streets *= 21
    with pytest.raises(PowerFetchError) as caught:
        await fixture.directory.streets(SEED, settlement, "Вулиця")
    assert caught.value.code is PowerFetchErrorCode.INVALID_RESPONSE


def test_unissued_manual_house_and_street_not_a_capability():
    fixture = DirectoryFixture()
    with pytest.raises(ValueError):
        fixture.directory._require(object())


@pytest.mark.parametrize("settlement_level", [2, 3])
async def test_oblast_seed_does_not_accept_settlement_category_at_administrative_level(
    settlement_level,
):
    fixture = DirectoryFixture()
    row = fixture.settlements[0]
    row["settlement"]["level"] = settlement_level
    if settlement_level == 2:
        row["settlement"]["parent_katottg"] = ROOT
        row["ancestors"] = [territory(ROOT, None, 1, "O", AREA)]
    else:
        row["settlement"]["parent_katottg"] = RAION
        row["ancestors"] = [territory(RAION, ROOT, 2, "P"),
                            territory(ROOT, None, 1, "O", AREA)]
    seed = PowerLocationSeed(ROOT, "OBLAST", "Synthetic oblast", "synthetic-v1")
    assert await fixture.directory.settlements(seed, "Синтетичне") == ()


async def test_oblast_seed_does_not_accept_level4_directly_under_oblast():
    fixture = DirectoryFixture()
    row = fixture.settlements[0]
    row["settlement"]["parent_katottg"] = ROOT
    row["ancestors"] = [territory(ROOT, None, 1, "O", AREA)]
    seed = PowerLocationSeed(ROOT, "OBLAST", "Synthetic oblast", "synthetic-v1")
    assert await fixture.directory.settlements(seed, "Синтетичне") == ()


@pytest.mark.parametrize("category", ["M", "C", "X"])
async def test_complete_ordinary_chain_is_preserved_for_oblast_seed(category):
    fixture = DirectoryFixture()
    fixture.settlements[0]["settlement"]["category"] = category
    seed = PowerLocationSeed(ROOT, "OBLAST", "Synthetic oblast", "synthetic-v1")
    choice, = await fixture.directory.settlements(seed, "Синтетичне")
    assert choice.katottg == CITY and choice.area_katottg == ROOT


@pytest.mark.parametrize("index,category", [(0, "M"), (1, "K"), (2, "H")])
async def test_ancestor_categories_must_match_exact_known_administrative_levels(index, category):
    fixture = DirectoryFixture()
    fixture.settlements[0]["ancestors"][index]["category"] = category
    assert await fixture.directory.settlements(SEED, "Синтетичне") == ()
