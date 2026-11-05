"""Version migration never opts old accounts into background delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from time_tl.admin.settings import reject_sensitive_generic_settings
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.db.power_map_preferences import PostgresPowerMapPreferences, decode
from time_tl.runtime.power_map_bot import PostgresMapPowerBotPort


def wire():
    return {
        "version": 2,
        "provider": "dtek_dnipro",
        "group": "6.1",
        "revision": 2,
        "enabled": True,
        "outage_enabled": True,
        "restoration_enabled": True,
        "outage_lead": 17,
        "restoration_lead": 10,
    }


def test_legacy_selection_remains_disabled_and_new_preferences_roundtrip():
    user = str(uuid4())
    old = decode({"version": 1, "provider": "dtek_dnipro", "group": "6.1", "revision": 1}, user)
    assert not old.enabled and not old.outage_notifications_enabled
    assert old.outage_lead_minutes == old.restoration_lead_minutes == 10
    new = decode(wire(), user)
    assert (
        new.enabled and new.outage_notifications_enabled and new.restoration_notifications_enabled
    )
    assert (new.outage_lead_minutes, new.restoration_lead_minutes, new.revision) == (17, 10, 2)


@pytest.mark.parametrize(
    "key,value",
    [
        ("enabled", 1),
        ("outage_enabled", "true"),
        ("restoration_enabled", None),
        ("outage_lead", True),
        ("outage_lead", -1),
        ("outage_lead", 181),
        ("restoration_lead", 1.5),
        ("revision", True),
        ("version", True),
        ("street", "must not persist"),
        ("provider", "dtek_kyiv"),
        ("group", "7.1"),
    ],
)
def test_invalid_or_private_preference_fields_rejected(key, value):
    data = wire()
    data[key] = value
    with pytest.raises(ValueError):
        decode(data, str(uuid4()))


async def test_explicit_runtime_capability_passes_exact_actor_and_custom_preferences():
    factory = Mock(side_effect=AssertionError("no direct SQL in port"))
    port = PostgresMapPowerBotPort(factory, notifications_enabled=True)
    port._preferences = Mock(save=AsyncMock(return_value="saved"))
    result = await port.save(
        42,
        PowerProvider.DTEK,
        "6.1",
        enabled=True,
        outage_enabled=True,
        restoration_enabled=False,
        outage_lead=17,
        restoration_lead=10,
        expected_revision=4,
    )
    assert result == "saved"
    port._preferences.save.assert_awaited_once_with(
        42,
        PowerProvider.DTEK,
        "6.1",
        enabled=True,
        outage_enabled=True,
        restoration_enabled=False,
        outage_lead=17,
        restoration_lead=10,
        expected_revision=4,
    )


def test_notification_cursor_is_not_a_generic_admin_setting():
    with pytest.raises(ValueError):
        reject_sensitive_generic_settings(("power_api_reminder_state:" + str(uuid4()),))


@pytest.mark.parametrize("legacy", [True, False])
async def test_optin_preview_defaults_do_not_enable_or_overwrite_saved_choices(legacy):
    user = uuid4()
    raw = (
        {"version": 1, "provider": "dtek_dnipro", "group": "6.1", "revision": 1}
        if legacy
        else {**wire(), "enabled": False, "outage_enabled": False, "restoration_enabled": False}
    )
    row = SimpleNamespace(value=dict(raw))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = session
    session.scalar = AsyncMock(return_value=user)
    session.get = AsyncMock(return_value=row)
    result = await PostgresPowerMapPreferences(Mock(return_value=session)).get(
        42,
        initialize_legacy_reminders=True,
    )
    assert result is not None and not result.enabled
    assert result.outage_notifications_enabled is legacy
    assert result.restoration_notifications_enabled is legacy
    assert row.value == raw
    session.add.assert_not_called()
