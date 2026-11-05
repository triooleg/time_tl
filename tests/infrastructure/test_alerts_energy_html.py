"""Offline selected-public-format and explicitly synthetic transport tests."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerPublicationStatus,
    PowerSourceKind,
    PowerState,
    SourceStatus,
)
from time_tl.infrastructure.power_outages.alerts_energy_html import (
    AlertsEnergyHtmlProvider,
    AlertsEnergyMode,
)
from time_tl.infrastructure.power_outages.alerts_energy_normalizer import (
    normalize_alerts_energy_page,
)
from time_tl.infrastructure.power_outages.alerts_energy_parser import (
    MAX_HTML_BYTES,
    AlertsEnergyParseError,
    parse_alerts_energy_html,
)
from time_tl.infrastructure.power_outages.base import PowerFetchError

NOW = datetime(2026, 11, 4, 14, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures/power_sources/alerts_energy_selected_cek.json"


def record() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["record"]


def html(rows: list[dict[str, object]] | None = None) -> bytes:
    state = {
        "site-access-v1": {"token": "SYNTHETIC_SHOULD_NOT_ESCAPE"},
        "api:/source-registry/areas/dnipropetrovska-oblast/shutdowns": {
            "data": rows if rows is not None else [record()],
            "ts": 123,
        },
    }
    return (
        '<script id="ng-state" type="application/json">' + json.dumps(state) + "</script>"
    ).encode()


class SyntheticTransport:
    synthetic: Literal[True] = True

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    async def get_html(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(PowerProvider))
async def test_live_policy_gate_precedes_any_io(provider: PowerProvider) -> None:
    transport = SyntheticTransport(html())
    result = await AlertsEnergyHtmlProvider(
        provider,
        synthetic_transport=transport,
        clock=lambda: NOW,
    ).fetch_schedules()
    assert result.status is SourceStatus.UNAVAILABLE
    assert result.source_kind is PowerSourceKind.ALERTS_ENERGY_HTML
    assert result.error_code == "SOURCE_POLICY_UNVERIFIED"
    assert not result.days and not transport.calls


def test_actual_scoped_emergency_overrides_schedule_applies() -> None:
    page = parse_alerts_energy_html(html(), region_slug="dnipropetrovska-oblast")
    assert "SYNTHETIC_SHOULD_NOT_ESCAPE" not in repr(page)
    result = normalize_alerts_energy_page(page, provider=PowerProvider.CEK, fetched_at=NOW)
    today, tomorrow = result.days
    assert today.publication_status is PowerPublicationStatus.EMERGENCY
    assert tomorrow.publication_status is PowerPublicationStatus.NO_SCHEDULE
    assert not tomorrow.intervals
    assert all(item.state is PowerState.UNKNOWN for day in result.days for item in day.intervals)
    assert result.source_updated_at != result.fetched_at
    assert result.error_code == "REFERENCE_UNVERIFIED"


def test_explicit_waiting_without_schedule_is_not_power_expected() -> None:
    row = record()
    row["todayStatus"] = "WaitingForSchedule"
    row["today"] = []
    row["todayAnnouncements"] = []
    result = normalize_alerts_energy_page(
        parse_alerts_energy_html(html([row]), region_slug="dnipropetrovska-oblast"),
        provider=PowerProvider.CEK,
        fetched_at=NOW,
    )
    assert result.days[0].publication_status is PowerPublicationStatus.NO_SCHEDULE
    assert not result.days[0].intervals


@pytest.mark.parametrize("slot", [0, 1, 2, 3])
def test_unverified_numeric_semantics_never_become_confirmed(slot: int) -> None:
    row = record()
    row["today"] = [slot] * 24
    row["todayAnnouncements"] = []
    result = normalize_alerts_energy_page(
        parse_alerts_energy_html(html([row]), region_slug="dnipropetrovska-oblast"),
        provider=PowerProvider.CEK,
        fetched_at=NOW,
    )
    assert result.days[0].publication_status is PowerPublicationStatus.UNKNOWN
    assert result.days[0].intervals[0].state is PowerState.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"x" * (MAX_HTML_BYTES + 1),
        b"\xff",
        b'<script id="ng-state" type="application/json">{"x":1,"x":2}</script>',
        b'<script id="ng-state" type="application/json">{"x":NaN}</script>',
        b'<script id="ng-state" type="application/json">' + b"[" * 100 + b"]" * 100 + b"</script>",
        b'<script id="ng-state" type="text/javascript">{}</script>',
        b'<script id="ng-state" type="application/json">{}</script>' * 2,
    ],
    ids=["empty", "oversized", "utf8", "duplicate", "constant", "depth", "type", "scripts"],
)
def test_invalid_or_unbounded_html_fails_closed(payload: bytes) -> None:
    with pytest.raises(AlertsEnergyParseError, match="invalid_alerts_energy_html"):
        parse_alerts_energy_html(payload, region_slug="dnipropetrovska-oblast")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("queue", "7.1"),
        ("today", [True] * 24),
        ("today", [4] * 24),
        ("today", [1] * 23),
        ("todayStatus", "NewUnrecognizedState"),
        ("todayDate", "2025-11-04"),
        ("updated", "2026-11-04T13:00:00"),
        ("updated", "2027-11-04T13:00:00Z"),
    ],
)
def test_invalid_schedule_fields_rejected(key: str, value: object) -> None:
    row = record()
    row[key] = value
    with pytest.raises(ValueError):
        normalize_alerts_energy_page(
            parse_alerts_energy_html(html([row]), region_slug="dnipropetrovska-oblast"),
            provider=PowerProvider.CEK,
            fetched_at=NOW,
        )


def test_operator_and_region_boundaries_and_duplicates() -> None:
    row = record()
    other = copy.deepcopy(row)
    other["initiator"] = "dnipropetrovska_oblast_oblenergo"
    page = parse_alerts_energy_html(html([row, other]), region_slug="dnipropetrovska-oblast")
    result = normalize_alerts_energy_page(page, provider=PowerProvider.CEK, fetched_at=NOW)
    assert len(result.days) == 2
    with pytest.raises(ValueError):
        normalize_alerts_energy_page(page, provider=PowerProvider.DTEK_KYIV, fetched_at=NOW)
    with pytest.raises(ValueError):
        normalize_alerts_energy_page(
            parse_alerts_energy_html(html([row, row]), region_slug="dnipropetrovska-oblast"),
            provider=PowerProvider.CEK,
            fetched_at=NOW,
        )


def test_node_budget_and_exact_regional_cache_path() -> None:
    excessive = (
        '<script id="ng-state" type="application/json">'
        + json.dumps({"irrelevant": [0] * 60_001})
        + "</script>"
    ).encode()
    for payload in (
        excessive,
        html().replace(b"dnipropetrovska-oblast/shutdowns", b"kyiv/shutdowns"),
    ):
        with pytest.raises(AlertsEnergyParseError):
            parse_alerts_energy_html(payload, region_slug="dnipropetrovska-oblast")


def test_unknown_fields_and_announcement_prose_not_retained() -> None:
    row = record()
    row["unexpected_auth"] = {"token": "SYNTHETIC_PRIVATE_MARKER"}
    row["todayAnnouncements"] = [
        {
            "kind": "Emergency",
            "state": "active",
            "message": "SYNTHETIC_PRIVATE_MARKER",
            "evidence": "https://example.invalid/private",
        }
    ]
    result = parse_alerts_energy_html(html([row]), region_slug="dnipropetrovska-oblast")
    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(result)
    assert "example.invalid" not in repr(result)


@pytest.mark.asyncio
async def test_synthetic_transport_only_fetches_public_html_and_bad_payload_is_closed() -> None:
    transport = SyntheticTransport(html())
    result = await AlertsEnergyHtmlProvider(
        PowerProvider.CEK,
        mode=AlertsEnergyMode.SYNTHETIC,
        synthetic_transport=transport,
        clock=lambda: NOW,
    ).fetch_schedules()
    assert result.days
    assert transport.calls == ["https://alerts.energy/dnipropetrovska-oblast"]
    with pytest.raises(ValueError):
        AlertsEnergyHtmlProvider(PowerProvider.CEK, mode=AlertsEnergyMode.SYNTHETIC)
    with pytest.raises(PowerFetchError):
        await AlertsEnergyHtmlProvider(
            PowerProvider.CEK,
            mode=AlertsEnergyMode.SYNTHETIC,
            synthetic_transport=SyntheticTransport(b"invalid"),
            clock=lambda: NOW,
        ).fetch_schedules()
