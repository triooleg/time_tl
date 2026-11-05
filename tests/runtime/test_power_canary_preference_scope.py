"""Only the verified public scope can enable a bounded preference preview."""

import base64
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from time_tl.runtime import power_telegram_canary, production_release


@pytest.mark.parametrize("value,expected", [(None, False), (False, False), (True, True)])
def test_verified_boolean_scope_defaults_closed(monkeypatch, value, expected):
    monkeypatch.setenv("POWER_MAP_REMINDERS_ENABLED", "true")
    record = {
        "kind": "time-tl-telegram-power-canary-overlay",
        "allowlist_count": 1,
        "maximum_window_seconds": 900,
        "messages_outside_allowlist": False,
        "source": {"files": {}},
    }
    if value is not None:
        record["map_reminder_preferences_enabled"] = value
    raw = json.dumps(record).encode()
    key = Mock()
    monkeypatch.setattr(production_release, "_public_key", lambda: key)

    def read(path):
        if path.name == "time-tl-power-telegram-overlay.json":
            return raw
        assert path.name == "time-tl-power-telegram-overlay.ed25519"
        return base64.b64encode(b"synthetic-signature-boundary")

    monkeypatch.setattr(Path, "read_bytes", read)
    assert power_telegram_canary.verify_overlay() is expected
    key.verify.assert_called_once_with(b"synthetic-signature-boundary", raw)


@pytest.mark.parametrize("value", [1, "true", None, {}, []])
def test_nonboolean_signed_preference_scope_rejected(monkeypatch, value):
    raw = json.dumps(
        {
            "kind": "time-tl-telegram-power-canary-overlay",
            "allowlist_count": 1,
            "maximum_window_seconds": 900,
            "messages_outside_allowlist": False,
            "map_reminder_preferences_enabled": value,
            "source": {"files": {}},
        }
    ).encode()
    monkeypatch.setattr(production_release, "_public_key", lambda: Mock())
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: raw if path.suffix == ".json" else base64.b64encode(b"s"),
    )
    with pytest.raises(ValueError, match="invalid_power_canary_attestation"):
        power_telegram_canary.verify_overlay()
