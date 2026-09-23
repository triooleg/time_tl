from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from time_tl.admin.settings import ActivationCandidate
from time_tl.entrypoints.operator_preflight import (
    OperatorPreflightError,
    probe_candidate,
)
from time_tl.infrastructure.alerts import HttpResponse


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
        return HttpResponse(200, {"Last-Modified": "marker"}, b'"NNNNNNNNNNNNNNNNNNNNNNNNNNN"')


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
    assert "covered_regions=27" in summary
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
            return HttpResponse(200, {}, b'"NNNNNNNNNNNNNNNNNNNNNNNNNNN"')

    bot = FakeBot("ignored")
    with pytest.raises(OperatorPreflightError, match="full-snapshot"):
        await probe_candidate(
            candidate(),
            123,
            bot_factory=lambda _token: bot,
            transport_factory=PartialTransport,
        )
    assert bot.session.closed
