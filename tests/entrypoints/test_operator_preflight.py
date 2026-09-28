from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from time_tl.admin.settings import ActivationCandidate
from time_tl.entrypoints.operator_preflight import (
    OperatorPreflightError,
    probe_candidate,
)
from time_tl.infrastructure.alerts import HttpResponse
from time_tl.runtime.region_registry_full import EXPECTED_RECORD_COUNT

MARKER = "Thu, 24 Sep 2026 10:00:00 GMT"


def full_body() -> bytes:
    return json.dumps(
        {
            "alerts": [
                {
                    "id": 10,
                    "location_title": "м. Київ",
                    "location_type": "city",
                    "started_at": "2026-09-24T09:00:00Z",
                    "finished_at": None,
                    "updated_at": "2026-09-24T09:01:00Z",
                    "alert_type": "air_raid",
                    "location_uid": "31",
                    "alert_level": "red",
                }
            ],
            "meta": {"last_updated_at": "2026/09/24 13:00:00 +0300"},
            "disclaimer": "Official alerts.in.ua data",
        }
    ).encode()


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self, _token: str) -> None:
        self.session = FakeSession()
        self.calls: list[str] = []

    async def get_me(self) -> SimpleNamespace:
        self.calls.append("get_me")
        return SimpleNamespace(is_bot=True, id=42, username="time_tl_staging_bot")


class FullTransport:
    async def send(self, _request: object) -> HttpResponse:
        return HttpResponse(200, {"Last-Modified": MARKER}, full_body())


def candidate(*, allowlist: tuple[int, ...] = (123,)) -> ActivationCandidate:
    return ActivationCandidate(
        revision=5,
        environment="staging",
        alert_provider="alerts_in_ua",
        telegram_transport="live",
        poll_interval_seconds=30,
        stale_after_seconds=120,
        staging_allowlist=allowlist,
        telegram_token=SecretStr("telegram-secret"),
        alerts_in_ua_token=SecretStr("alerts-secret"),
    )


@pytest.mark.asyncio
async def test_probe_is_read_only_and_summary_contains_no_secrets_or_ids() -> None:
    bots: list[FakeBot] = []

    def bot_factory(token: str) -> FakeBot:
        bot = FakeBot(token)
        bots.append(bot)
        return bot

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=bot_factory,
        transport_factory=FullTransport,
    )

    assert bots[0].calls == ["get_me"]
    assert bots[0].session.closed
    summary = evidence.sanitized_summary()
    assert "revision=5" in summary
    assert f"covered_regions={EXPECTED_RECORD_COUNT}" in summary
    assert "alerts=active_json_full_200" in summary
    assert "persisted=no" in summary
    assert "123" not in summary
    assert "secret" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", [(), (999,), (123, 999)])
async def test_probe_rejects_any_non_exact_allowlist(allowlist: tuple[int, ...]) -> None:
    with pytest.raises(OperatorPreflightError, match="exactly match"):
        await probe_candidate(
            candidate(allowlist=allowlist),
            123,
            bot_factory=FakeBot,
            transport_factory=FullTransport,
        )


@pytest.mark.asyncio
async def test_probe_rejects_partial_provider_response_after_closing_bot() -> None:
    class PartialTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(200, {}, full_body())

    bot = FakeBot("ignored")
    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=lambda _token: bot,
            transport_factory=PartialTransport,
        )
    assert bot.session.closed
    assert "kind=PARTIAL_RESPONSE code=last_modified_missing count=1" in str(captured.value)


@pytest.mark.asyncio
async def test_probe_rejects_malformed_last_modified_marker() -> None:
    class MalformedMarkerTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(200, {"Last-Modified": "not-an-http-date"}, full_body())

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=MalformedMarkerTransport,
        )
    message = str(captured.value)
    assert "kind=INVALID_RESPONSE code=last_modified_invalid count=1" in message
    assert "not-an-http-date" not in message
    assert "secret" not in message


@pytest.mark.asyncio
async def test_probe_ignores_bounded_undocumented_field_without_exposing_it() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class DriftTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=DriftTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "private_provider_field" not in summary
    assert "must-not-leak" not in summary
    assert "telegram-secret" not in summary
    assert "alerts-secret" not in summary


@pytest.mark.asyncio
async def test_probe_does_not_use_previously_diagnostic_only_alert_field() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["calculated"] = "private-value-must-not-leak"

    class DiagnosticCandidateTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=DiagnosticCandidateTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "calculated" not in summary
    assert "private-value-must-not-leak" not in summary
    assert "123" not in summary
    assert "telegram-secret" not in summary
    assert "alerts-secret" not in summary


@pytest.mark.asyncio
async def test_probe_accepts_matching_integer_oblast_uid_without_exposing_it() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["location_oblast_uid"] = 31
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class IntegerOblastUidTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=IntegerOblastUidTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "location_oblast_uid" not in summary
    assert "private_provider_field" not in summary
    assert "must-not-leak" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "code"),
    [
        (-31, "location_oblast_uid_integer"),
        (0, "location_oblast_uid_integer"),
        (10**100, "location_oblast_uid_unknown"),
        ("031", "location_oblast_uid_noncanonical_string"),
        (16, "location_oblast_uid_hierarchy_mismatch"),
        (True, "location_oblast_uid_boolean"),
        (31.0, "location_oblast_uid_other"),
    ],
)
async def test_probe_rejects_invalid_oblast_uid_with_sanitized_category(
    value: object,
    code: str,
) -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["location_oblast_uid"] = value
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class InvalidOblastUidTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=InvalidOblastUidTransport,
        )

    message = str(captured.value)
    assert f"kind=INVALID_RESPONSE code={code} count=1" in message
    assert str(value) not in message
    assert "private_provider_field" not in message
    assert "must-not-leak" not in message
    assert "telegram-secret" not in message
    assert "alerts-secret" not in message


@pytest.mark.asyncio
async def test_probe_rejects_malformed_official_metadata_without_leaking_value() -> None:
    payload = json.loads(full_body())
    payload["meta"]["last_updated_at"] = "private-invalid-timestamp"

    class InvalidMetadataTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=InvalidMetadataTransport,
        )

    message = str(captured.value)
    assert "code=meta_last_updated_at_format_invalid count=1" in message
    assert "private-invalid-timestamp" not in message
    assert "telegram-secret" not in message
    assert "alerts-secret" not in message
