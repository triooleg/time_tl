"""Synthetic API checked-at evidence is distinct from unchanged publication time."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerProvider,
    PowerPublicationStatus,
    PowerSourceKind,
    SourceStatus,
)
from time_tl.domain.power_outages.sources import _fresh, select_snapshot
from time_tl.infrastructure.db.models import RuntimeSetting
from time_tl.infrastructure.db.power import PostgresPowerRepository
from time_tl.infrastructure.db.power_sources import (
    PostgresPowerSourceRepository,
    decode_snapshot,
    encode_snapshot,
    initial_state,
)
from time_tl.infrastructure.power_outages.alerts_energy_api_normalizer import (
    normalize_api_schedules,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PUBLICATION = NOW - timedelta(hours=20)
CHECKED = NOW - timedelta(minutes=5)


def records():
    return [
        {
            "initiator": "dnipropetrovska_oblast_oblenergo",
            "queue": group,
            "updated": PUBLICATION.isoformat(),
            "today": [0, 1] + [0] * 22,
            "today_date": "2026-11-04",
            "today_status": "ScheduleApplies",
            "today_checked_at": CHECKED.isoformat(),
            "tomorrow": [],
            "tomorrow_date": "2026-11-05",
            "tomorrow_status": "WaitingForSchedule",
        }
        for group in GROUPS
    ]


def normalize(value):
    return normalize_api_schedules(
        value,
        provider=PowerProvider.DTEK,
        area_slug="dnipropetrovska-oblast",
        supplier_key="dnipropetrovska_oblast_oblenergo",
        fetched_at=NOW,
    )


def test_old_publication_recent_complete_checks_fresh_and_waiting_tomorrow_does_not_poison():
    value = records()
    value[-1]["today_checked_at"] = (CHECKED - timedelta(seconds=30)).isoformat()
    snapshot = normalize(value)
    assert snapshot.status is SourceStatus.FRESH
    assert snapshot.source_updated_at == PUBLICATION
    assert snapshot.source_checked_at == CHECKED - timedelta(seconds=30)
    assert _fresh(snapshot, NOW, 1800)
    waiting = [day for day in snapshot.days if day.local_date.isoformat() == "2026-11-05"]
    assert len(waiting) == 12
    assert all(
        day.publication_status is PowerPublicationStatus.NO_SCHEDULE and not day.intervals
        for day in waiting
    )
    selected = select_snapshot(PowerProvider.DTEK, (snapshot,), NOW)
    assert selected.snapshot == snapshot and not selected.notification_authorized


@pytest.mark.parametrize("checked", [None, NOW - timedelta(minutes=31)])
def test_recent_publication_cannot_hide_one_group_missing_or_stale_check(checked):
    value = records()
    for row in value:
        row["updated"] = NOW.isoformat()
    value[-1]["today_checked_at"] = checked.isoformat() if checked else None
    snapshot = normalize(value)
    assert snapshot.status is SourceStatus.STALE and not snapshot.days
    assert not _fresh(snapshot, NOW, 1800)


def test_published_tomorrow_requires_its_own_complete_check_evidence():
    value = records()
    value[0].update(tomorrow=[0] * 24, tomorrow_status="NoShutdowns")
    assert normalize(value).status is SourceStatus.STALE
    value[0]["tomorrow_checked_at"] = CHECKED.isoformat()
    assert normalize(value).status is SourceStatus.FRESH


@pytest.mark.parametrize("checked", ["2026-11-04T12:00:01Z", "2026-11-04T12:00:00", False])
def test_future_naive_or_non_timestamp_check_rejected(checked):
    value = records()
    value[0]["today_checked_at"] = checked
    with pytest.raises(ValueError):
        normalize(value)


def test_checked_and_fetched_ttls_both_enforced_without_rewriting_publication():
    snapshot = normalize(records())
    assert not _fresh(snapshot, NOW + timedelta(minutes=26), 1800)
    assert not _fresh(
        replace(
            snapshot,
            fetched_at=NOW - timedelta(minutes=31),
            source_checked_at=NOW - timedelta(minutes=32),
        ),
        NOW,
        1800,
    )
    assert not _fresh(replace(snapshot, source_checked_at=None), NOW, 1800)
    # Other adapters retain their original publication-based freshness contract.
    official = replace(snapshot, source_kind=PowerSourceKind.OFFICIAL, source_url=None)
    assert not _fresh(official, NOW, 1800)


def test_new_snapshot_timestamp_aware_and_not_after_fetch():
    snapshot = normalize(records())
    for invalid in (NOW + timedelta(seconds=1), NOW.replace(tzinfo=None), False, "bad"):
        with pytest.raises(ValueError):
            replace(snapshot, source_checked_at=invalid)


def test_checked_json_roundtrip_and_legacy_v1_missing_field_conservatively_stale():
    snapshot = normalize(records())
    encoded = encode_snapshot(snapshot)
    assert decode_snapshot(encoded) == snapshot
    del encoded["source_checked_at"]
    legacy = decode_snapshot(encoded)
    assert legacy.source_checked_at is None and not _fresh(legacy, NOW, 1800)
    encoded["unknown_checked_alias"] = CHECKED.isoformat()
    with pytest.raises(ValueError, match="shape"):
        decode_snapshot(encoded)


async def test_recent_checks_never_admit_api_to_official_outbox():
    def forbidden():
        raise AssertionError("official-only rejection must precede DB access")

    with pytest.raises(ValueError, match="nonofficial"):
        await PostgresPowerRepository(forbidden).store_snapshot(normalize(records()), NOW)


async def test_public_cache_status_uses_checked_at_and_legacy_cache_stays_stale():
    snapshot = normalize(records())
    value = initial_state(PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_API)
    value.update(snapshot=encode_snapshot(snapshot), source_status="FRESH")
    key = "power_source:dtek_dnipro:alerts_energy_api"
    row = RuntimeSetting(key=key, value=value, updated_by="test")
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = AsyncMock(side_effect=lambda model, requested: row if requested == key else None)
    repo = PostgresPowerSourceRepository(MagicMock(return_value=session))

    async def status(at):
        states = await repo.source_states(at)
        return next(
            item
            for item in states
            if item["provider_id"] == "dtek_dnipro" and item["source_kind"] == "alerts_energy_api"
        )

    assert (await status(NOW))["source_status"] == "FRESH"
    assert (await status(NOW + timedelta(minutes=26)))["source_status"] == "STALE"
    del value["snapshot"]["source_checked_at"]
    assert (await status(NOW))["source_status"] == "STALE"
