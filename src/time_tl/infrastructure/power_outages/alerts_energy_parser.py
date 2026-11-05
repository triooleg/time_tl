"""Bounded offline extraction of public Angular SSR schedule records only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_HTML_BYTES = 2_097_152
MAX_JSON_NODES = 60_000
MAX_JSON_DEPTH = 32
REGIONS = frozenset(
    {
        "dnipropetrovska-oblast",
        "kyiv",
        "kyivska-oblast",
        "odeska-oblast",
        "donetska-oblast",
    }
)
RECORD_FIELDS = frozenset(
    {
        "initiator",
        "queue",
        "updated",
        "today",
        "tomorrow",
        "todayStatus",
        "tomorrowStatus",
        "todayDate",
        "tomorrowDate",
        "todayPossible",
        "tomorrowPossible",
        "todayAnnouncements",
        "tomorrowAnnouncements",
        "todayCheckedAt",
        "tomorrowCheckedAt",
        "todaySourceSlug",
        "tomorrowSourceSlug",
    }
)
ANNOUNCEMENT_FIELDS = frozenset(
    {
        "kind",
        "state",
        "scope",
        "effectiveAt",
        "expiresAt",
        "publishedAt",
        "timeBasis",
    }
)


class AlertsEnergyParseError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_alerts_energy_html")


@dataclass(frozen=True)
class ParsedAlertsEnergyPage:
    region_slug: str
    records: tuple[dict[str, object], ...]


class _StateHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.seen = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        if len({key for key, _ in attrs}) != len(attrs):
            raise AlertsEnergyParseError
        values = dict(attrs)
        if values.get("id") == "ng-state":
            if self.seen or values.get("type") != "application/json" or "src" in values:
                raise AlertsEnergyParseError
            self.inside = self.seen = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.parts.append(data)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise AlertsEnergyParseError
        output[key] = value
    return output


def _reject_constant(_: str) -> object:
    raise AlertsEnergyParseError


def _validate_tree(value: object) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise AlertsEnergyParseError
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def parse_alerts_energy_html(payload: bytes, *, region_slug: str) -> ParsedAlertsEnergyPage:
    """Never return transfer-cache URLs, site-access tokens, or arbitrary state."""
    if type(payload) is not bytes or not payload or len(payload) > MAX_HTML_BYTES:
        raise AlertsEnergyParseError
    if region_slug not in REGIONS:
        raise AlertsEnergyParseError
    try:
        parser = _StateHtmlParser()
        parser.feed(payload.decode("utf-8", errors="strict"))
        parser.close()
        if not parser.seen or parser.inside:
            raise AlertsEnergyParseError
        state = json.loads(
            "".join(parser.parts),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
        _validate_tree(state)
        if type(state) is not dict:
            raise AlertsEnergyParseError
        # This is a literal SSR cache key, never a requested network endpoint.
        entry = state.get(f"api:/source-registry/areas/{region_slug}/shutdowns")
        if type(entry) is not dict:
            raise AlertsEnergyParseError
        records = entry.get("data")
        if type(records) is not list or not 1 <= len(records) <= 512:
            raise AlertsEnergyParseError
        selected: list[dict[str, object]] = []
        for record in records:
            if type(record) is not dict:
                raise AlertsEnergyParseError
            clean = {key: value for key, value in record.items() if key in RECORD_FIELDS}
            for day in ("today", "tomorrow"):
                key = f"{day}Announcements"
                if key not in clean:
                    continue
                announcements = clean[key]
                if type(announcements) is not list or len(announcements) > 64:
                    raise AlertsEnergyParseError
                projected: list[dict[str, object]] = []
                for item in announcements:
                    if type(item) is not dict:
                        raise AlertsEnergyParseError
                    projected.append({k: v for k, v in item.items() if k in ANNOUNCEMENT_FIELDS})
                clean[key] = projected
            selected.append(clean)
        return ParsedAlertsEnergyPage(region_slug, tuple(selected))
    except (UnicodeError, RecursionError, json.JSONDecodeError, OverflowError):
        raise AlertsEnergyParseError from None
