"""Provider alert UID and a community are not settlement address identifiers."""

import pytest

from time_tl.domain.power_outages.location import PowerLocationSeed


@pytest.mark.parametrize("kind", ["OBLAST", "RAION", "HROMADA"])
def test_existing_broad_geography_is_reused_as_hint_but_needs_settlement(kind):
    seed = PowerLocationSeed("UA12000000000090473", kind, "Синтетична територія", "test-v1")
    assert seed.katottg == "UA12000000000090473"
    assert seed.needs_settlement


def test_confirmed_special_status_city_can_seed_address_lookup():
    seed = PowerLocationSeed("UA80000000000093317", "SPECIAL_STATUS_CITY", "Київ", "test-v1")
    assert not seed.needs_settlement


@pytest.mark.parametrize("invalid", ["5272", "272", "Kamjanske", "UA123", "UA" + "１" * 17])
def test_alert_ids_names_and_unmapped_codes_cannot_be_used_as_katottg(invalid):
    with pytest.raises(ValueError, match="mapping_unavailable"):
        PowerLocationSeed(invalid, "HROMADA", "Синтетична територія", "test-v1")
