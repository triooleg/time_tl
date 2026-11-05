"""SYNTHETIC technical-reference fixtures; no live requests or official claims."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from time_tl.domain.power_outages.models import GROUPS, PowerProvider, PowerState, SourceStatus
from time_tl.domain.power_outages.normalization import KYIV
from time_tl.infrastructure.power_outages import PowerFetchError, PowerFetchErrorCode
from time_tl.infrastructure.power_outages.dtek_html_provider import (
    DTEK_REGIONS,
    DtekHtmlProvider,
    HtmlResponse,
    HttpxHtmlTransport,
    inspect_html_fact,
    read_html_response,
)
from time_tl.infrastructure.power_outages.dtek_normalizer import normalize_dtek_fact
from time_tl.infrastructure.power_outages.dtek_parser import (
    MAX_HTML_BYTES,
    DtekDataError,
    extract_fact_object,
    parse_dtek_fact,
)

NOW = datetime(2026, 11, 4, 10, 5, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures/power_sources/dtek-reference.synthetic.html"


def synthetic_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def synthetic_object() -> dict:
    return extract_fact_object(synthetic_html())


def html_for(data: dict) -> str:
    return "<!-- SYNTHETIC --><script>var fact = " + json.dumps(data) + ";</script>"


class FakeHtmlTransport:
    def __init__(self, response: HtmlResponse | BaseException):
        self.response = response
        self.calls: list[str] = []

    async def fetch(self, url: str) -> HtmlResponse:
        self.calls.append(url)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def offline_provider(transport: FakeHtmlTransport, **kwargs) -> DtekHtmlProvider:
    return DtekHtmlProvider(
        transport=transport, offline_environment="test", fixture_kind="SYNTHETIC",
        clock=lambda: NOW, **kwargs,
    )


def test_extracts_only_script_literal_with_balanced_escaped_nested_objects():
    value = {"nested": {"escaped": "brace } \\\" [ string", "list": [1, {"x": 2}]}}
    html = 'fact = {"outside":true};<script>const fact=' + json.dumps(value) + ";</script>"
    assert extract_fact_object(html) == value


@pytest.mark.parametrize("prefix", [
    '// fact={"false":true};\n',
    '<!-- var fact={"false":true}; -->\n',
    '/* fact={"false":true}; */',
    'const text = "fact={}";',
    "const text = 'fact={};';",
    'const text = `fact={};`;',
    r'const pattern = /fact\s*={}/;',
])
def test_comment_string_template_and_regex_false_positives_are_ignored(prefix):
    assert extract_fact_object('<script>' + prefix + 'var fact={"good":true};</script>') == {
        "good": True
    }


@pytest.mark.parametrize("html", [
    '<p>fact={"outside":true};</p>',
    '<script>const facture={};</script>',
    '<script src="/remote.js">fact={};</script>',
    '<script type="application/ld+json">{"fact":{}}</script>',
    '<script>const fact=loadData();</script>',
    '<script>fact={} + {"extra":true};</script>',
    '<script>fact={"data":1,"data":2};</script>',
    '<script>fact={"data":NaN};</script>',
    '<script>fact={};fact={};</script>',
    '<script>fact={};</script><script>fact={};</script>',
    '<script>fact={"unclosed":{};</script>',
    '<script>fact={};',
])
def test_rejects_ambiguous_nonliteral_missing_or_invalid_fact(html):
    with pytest.raises(DtekDataError):
        extract_fact_object(html)


def test_size_and_depth_limits_fail_closed():
    with pytest.raises(DtekDataError):
        extract_fact_object(b"x" * (MAX_HTML_BYTES + 1))
    with pytest.raises(DtekDataError):
        extract_fact_object('<script>fact={"deep":' + '[' * 33 + '0' + ']' * 33 + '};</script>')


def test_complete_all_groups_normalizes_exact_half_hours_and_keeps_unknowns():
    fact = parse_dtek_fact(synthetic_html())
    snapshot = normalize_dtek_fact(
        fact, provider_id=PowerProvider.DTEK, fetched_at=NOW, synthetic_profile=True
    )
    assert snapshot.status is SourceStatus.FRESH and len(snapshot.days) == 12
    assert {day.group_id for day in snapshot.days} == set(GROUPS)
    group = next(day for day in snapshot.days if day.group_id == "3.2")
    values = [(item.starts_at.astimezone(KYIV).strftime("%H:%M"),
               item.ends_at.astimezone(KYIV).strftime("%H:%M"), item.state)
              for item in group.intervals]
    assert ("08:00", "08:30", PowerState.OUTAGE_CONFIRMED) in values
    assert ("09:00", "10:00", PowerState.OUTAGE_CONFIRMED) in values
    assert ("10:30", "11:00", PowerState.OUTAGE_CONFIRMED) in values
    assert ("11:00", "15:00", PowerState.UNKNOWN) in values
    assert all(item.starts_at < item.ends_at for item in group.intervals)
    assert len(next(day for day in snapshot.days if day.group_id == "1.1").intervals) == 1
    assert snapshot.source_updated_at == datetime(2026, 11, 4, 10, tzinfo=UTC)


@pytest.mark.parametrize("case", ["missing_group", "extra_group", "hour23", "hour25", "no_today",
                                  "bad_update", "naive_update", "bool_today", "numeric_state"])
def test_partial_groups_hours_dates_or_invalid_types_reject_complete_snapshot(case):
    value = synthetic_object()
    groups = next(iter(value["data"].values()))
    if case == "missing_group":
        del groups["GPV1.1"]
    elif case == "extra_group":
        groups["GPV7.1"] = groups["GPV1.1"]
    elif case == "hour23":
        del groups["GPV1.1"]["24"]
    elif case == "hour25":
        groups["GPV1.1"]["25"] = "yes"
    elif case == "no_today":
        value["today"] += 86400
    elif case == "bad_update":
        value["update"] = "unparseable"
    elif case == "naive_update":
        value["update"] = "2026-11-04T12:00:00"
    elif case == "bool_today":
        value["today"] = True
    else:
        groups["GPV1.1"]["1"] = 1
    with pytest.raises(DtekDataError):
        parse_dtek_fact(html_for(value))


@pytest.mark.parametrize("day", [datetime(2026, 3, 29, tzinfo=KYIV),
                                datetime(2026, 10, 25, tzinfo=KYIV)])
def test_24_hour_wire_cannot_invent_missing_or_repeated_dst_hour(day):
    value = synthetic_object()
    groups = next(iter(value["data"].values()))
    epoch = int(day.timestamp())
    value["today"], value["data"] = epoch, {str(epoch): groups}
    with pytest.raises(DtekDataError, match="AMBIGUOUS_24_HOUR_DAY"):
        parse_dtek_fact(html_for(value))


def test_unverified_semantics_cannot_be_implicitly_normalized():
    with pytest.raises(DtekDataError, match="UNVERIFIED"):
        normalize_dtek_fact(parse_dtek_fact(synthetic_html()), provider_id=PowerProvider.DTEK,
                            fetched_at=NOW)


def test_unknown_uppercase_state_is_unknown_not_automatically_power_expected():
    value = synthetic_object()
    next(iter(value["data"].values()))["GPV1.1"]["1"] = "YES"
    snapshot = normalize_dtek_fact(
        parse_dtek_fact(html_for(value)), provider_id=PowerProvider.DTEK,
        fetched_at=NOW, synthetic_profile=True,
    )
    assert next(day for day in snapshot.days if day.group_id == "1.1").intervals[0].state is (
        PowerState.UNKNOWN
    )


def test_future_source_time_rejects_and_old_source_is_stale_not_fresh():
    fact = parse_dtek_fact(synthetic_html())
    with pytest.raises(DtekDataError, match="FUTURE"):
        normalize_dtek_fact(fact, provider_id=PowerProvider.DTEK,
                            fetched_at=NOW - timedelta(hours=1), synthetic_profile=True)
    stale = normalize_dtek_fact(fact, provider_id=PowerProvider.DTEK,
                               fetched_at=NOW + timedelta(hours=1), synthetic_profile=True)
    assert stale.status is SourceStatus.STALE and not stale.days


def test_technical_diagnostic_is_pure_and_never_claims_readiness():
    result = inspect_html_fact(synthetic_html())
    assert result["dates"] == ["2026-11-04"] and result["group_count"] == [12]
    assert result["hours_complete"] is True
    assert result["ready"] is result["semantics_verified"] is False


@pytest.mark.parametrize("region", DTEK_REGIONS)
async def test_every_live_region_is_gate_closed_even_with_injected_transport(region):
    transport = FakeHtmlTransport(AssertionError("source gate must precede transport"))
    provider = DtekHtmlProvider(region=region, transport=transport, clock=lambda: NOW)
    result = await provider.fetch_schedules()
    assert result.status is SourceStatus.UNAVAILABLE and not result.days
    assert result.provider is DTEK_REGIONS[region] and transport.calls == []
    with pytest.raises(PowerFetchError, match="SOURCE_POLICY_UNVERIFIED"):
        await HttpxHtmlTransport(provider.source_gate).fetch(provider.provider_id.official_url)


async def test_one_synthetic_response_supplies_all_groups_without_cross_region_substitution():
    transport = FakeHtmlTransport(HtmlResponse(200, synthetic_html().encode()))
    provider = offline_provider(transport, region="o")
    result = await provider.fetch_schedules()
    assert result.status is SourceStatus.FRESH and len(result.days) == 12
    assert result.provider is PowerProvider.DTEK_ODESA
    assert transport.calls == [PowerProvider.DTEK_ODESA.official_url]


async def test_explicit_synthetic_browser_adapter_works_without_playwright_dependency():
    browser = FakeHtmlTransport(HtmlResponse(200, synthetic_html().encode()))
    result = await DtekHtmlProvider(
        region="kr", browser_adapter=browser, offline_environment="simulation",
        fixture_kind="SYNTHETIC", clock=lambda: NOW,
    ).fetch_schedules()
    assert result.status is SourceStatus.FRESH and len(browser.calls) == 1


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_http_failure_is_single_attempt_no_browser_fallback(status):
    transport = FakeHtmlTransport(HtmlResponse(status, b"untrusted", 30))
    with pytest.raises(PowerFetchError) as raised:
        await offline_provider(transport).fetch_schedules()
    assert raised.value.status_code == status and len(transport.calls) == 1
    if status == 403:
        assert raised.value.permanent
    if status == 429:
        assert raised.value.retry_after_seconds == 30


async def test_protection_page_is_invalid_not_fresh_and_cancellation_is_propagated():
    transport = FakeHtmlTransport(HtmlResponse(200, b"<html>Access challenge</html>"))
    result = await offline_provider(transport).fetch_schedules()
    assert result.status is SourceStatus.INVALID and not result.days
    cancelled = FakeHtmlTransport(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await offline_provider(cancelled).fetch_schedules()


async def test_timeout_is_bounded_single_attempt_no_fallback():
    transport = FakeHtmlTransport(TimeoutError())
    with pytest.raises(PowerFetchError) as raised:
        await offline_provider(transport).fetch_schedules()
    assert raised.value.code is PowerFetchErrorCode.TIMEOUT and len(transport.calls) == 1


@pytest.mark.parametrize("advertised", [False, True])
async def test_supplied_http_response_size_is_bounded_even_without_content_length(advertised):
    headers = {"Content-Length": str(MAX_HTML_BYTES + 1)} if advertised else {}
    response = httpx.Response(200, headers=headers, content=b"x" * (MAX_HTML_BYTES + 1))
    with pytest.raises(PowerFetchError, match="INVALID_RESPONSE"):
        await read_html_response(response)


async def test_429_http_date_and_numeric_retry_after_are_respected_without_reading_body():
    for header in ("30", "Wed, 04 Nov 2026 10:05:30 GMT"):
        response = httpx.Response(429, headers={"Retry-After": header}, content=b"PRIVATE")
        result = await read_html_response(response, clock=lambda: NOW)
        assert result.retry_after_seconds == 30 and result.body == b""
    excessive = httpx.Response(429, headers={"Retry-After": "86401"})
    with pytest.raises(PowerFetchError) as raised:
        await read_html_response(excessive)
    assert raised.value.permanent
    huge = httpx.Response(429, headers={"Retry-After": "9" * 129})
    with pytest.raises(PowerFetchError) as huge_error:
        await read_html_response(huge)
    assert huge_error.value.permanent


@pytest.mark.parametrize("environment", ["production", "staging", "", "TEST"])
def test_synthetic_transport_cannot_be_selected_in_live_environment(environment):
    with pytest.raises(ValueError):
        DtekHtmlProvider(offline_environment=environment, fixture_kind="SYNTHETIC",
                         transport=FakeHtmlTransport(HtmlResponse(200, b"")))


def test_region_unknown_and_ambiguous_modes_refuse_instead_of_defaulting_kyiv():
    with pytest.raises(ValueError):
        DtekHtmlProvider(region="unknown")
    with pytest.raises(ValueError):
        DtekHtmlProvider(PowerProvider.DTEK, region="o")
    with pytest.raises(ValueError):
        DtekHtmlProvider(transport=FakeHtmlTransport(HtmlResponse(200, b"")),
                         browser_adapter=FakeHtmlTransport(HtmlResponse(200, b"")))
