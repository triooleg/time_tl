"""Pure persistence-contract checks; no PostgreSQL/server or network required."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
    SourceStatus,
)
from time_tl.infrastructure.db.models import RuntimeSetting
from time_tl.infrastructure.db.power_sources import (
    PostgresPowerSourceRepository,
    PowerSourceConflict,
    decode_snapshot,
    encode_snapshot,
    initial_state,
    request_refresh_state,
    validate_state,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def test_normalized_unavailable_roundtrip_without_html_or_keys() -> None:
    value = PowerScheduleSnapshot(PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE,
                                  "SOURCE_POLICY_UNVERIFIED")
    assert decode_snapshot(encode_snapshot(value)) == value
    raw = encode_snapshot(value)
    raw["html"] = "untrusted"
    with pytest.raises(ValueError, match="shape"):
        decode_snapshot(raw)


@pytest.mark.parametrize("field,value", [
    ("collection_allowed", True), ("notifications_allowed", True),
    ("verification_status", "MATCH"), ("matched_count", 1), ("compared_count", 12),
    ("collection_allowed", 0), ("matched_count", False),
])
def test_persisted_configuration_cannot_self_approve(field: str, value: object) -> None:
    state = initial_state(PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML)
    state[field] = value
    with pytest.raises(ValueError, match="approval"):
        validate_state(state, PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML)


def test_refresh_coalesces_across_copied_persistent_state_and_cooldown() -> None:
    state = initial_state(PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML)
    assert request_refresh_state(state, NOW) is True
    restarted = deepcopy(state)
    assert request_refresh_state(restarted, NOW + timedelta(seconds=1)) is False
    restarted["refresh_requested"] = False
    assert request_refresh_state(restarted, NOW + timedelta(seconds=299)) is False
    assert request_refresh_state(restarted, NOW + timedelta(seconds=300)) is True
    assert restarted["collection_allowed"] is False


def test_query_string_cannot_persist_address_or_credentials() -> None:
    value = PowerScheduleSnapshot(
        PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE,
        source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
        source_url="https://alerts.energy/?token=not-a-real-token",
    )
    with pytest.raises(ValueError, match="query"):
        encode_snapshot(value)


def repository_harness() -> tuple[PostgresPowerSourceRepository, RuntimeSetting, list[Any]]:
    """Mock ORM boundary only; does not claim PostgreSQL locking evidence."""
    row = RuntimeSetting(key="test", value=initial_state(
        PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML), updated_by="test")
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = session
    audit: list[Any] = []
    session.add.side_effect = audit.append
    session.get = AsyncMock(return_value=row)
    repository = PostgresPowerSourceRepository(MagicMock(return_value=session))
    repository._locked = AsyncMock(return_value=row)  # type: ignore[method-assign]
    return repository, row, audit


@pytest.mark.asyncio
async def test_enable_cas_audit_and_closed_slot() -> None:
    repo, row, audit = repository_harness()
    revision = await repo.set_source_enabled(
        PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML, True,
        expected_revision=1, actor_id="operator", now=NOW,
    )
    assert revision == 2 and row.value["enabled"] is True
    assert row.value["collection_allowed"] is False
    assert len(audit) == 1 and audit[0].action == "power_source_enabled"
    assert audit[0].redacted_details["collection_allowed"] is False
    with pytest.raises(PowerSourceConflict):
        await repo.set_source_enabled(
            PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML, False,
            expected_revision=1, actor_id="operator", now=NOW,
        )
    assert row.value["enabled"] is True and len(audit) == 1
    assert await repo.acquire_source_slot(
        PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML, NOW, 300,
    ) is None


@pytest.mark.asyncio
async def test_refresh_persisted_coalesced_and_audited() -> None:
    repo, row, audit = repository_harness()
    assert await repo.request_source_refresh(
        PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML, actor_id="operator", now=NOW,
    ) is True
    assert await repo.request_source_refresh(
        PowerProvider.DTEK, PowerSourceKind.ALERTS_ENERGY_HTML, actor_id="operator", now=NOW,
    ) is False
    assert row.value["revision"] == 2 and row.value["refresh_requested"] is True
    assert [item.redacted_details["coalesced"] for item in audit] == [False, True]


@pytest.mark.asyncio
async def test_failed_observation_persists_without_false_success_or_verified_samples() -> None:
    repo, row, _ = repository_harness()
    observation = PowerScheduleSnapshot(
        PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE,
        "SOURCE_POLICY_UNVERIFIED", source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
    )
    await repo.record_observation(observation, None, NOW)
    await repo.record_observation(observation, None, NOW)
    assert row.value["last_success_at"] is None
    assert row.value["last_attempt_at"] == NOW.isoformat()
    assert row.value["verification_status"] == "UNVERIFIED"
    assert row.value["matched_count"] == row.value["compared_count"] == 0
    assert decode_snapshot(row.value["snapshot"]) == observation
    with pytest.raises(ValueError, match="future"):
        await repo.record_observation(observation, None, NOW - timedelta(seconds=1))
