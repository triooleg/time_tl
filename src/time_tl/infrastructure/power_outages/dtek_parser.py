"""Bounded static extraction of a literal fact object; never executes JavaScript.

The currently supported wire profile comes from the supplied technical reference,
not verified official source semantics. Parsing it does not open a source gate.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import cast

from time_tl.domain.power_outages.models import GROUPS
from time_tl.domain.power_outages.normalization import KYIV, day_bounds, local_boundary

MAX_HTML_BYTES = 2097152
MAX_SCRIPT_BYTES = 524288
MAX_SCRIPTS = 64
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 20000
REFERENCE_MAPPING_VERSION = "reference-v1-synthetic"
EXTERNAL_GROUPS = tuple("GPV" + group for group in GROUPS)
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class DtekDataError(ValueError):
    """A fixed error code, never HTML, provider text or an arbitrary field value."""


@dataclass(frozen=True)
class DtekRawGroup:
    group_id: str
    hours: tuple[str, ...]


@dataclass(frozen=True)
class DtekRawDay:
    day: date
    groups: tuple[DtekRawGroup, ...]


@dataclass(frozen=True)
class DtekFact:
    today: date
    source_updated_at: datetime
    days: tuple[DtekRawDay, ...]
    mapping_version: str = REFERENCE_MAPPING_VERSION


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise DtekDataError(code)


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self.current: list[str] | None = None
        self.eligible = False
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            _need(self.current is None, "INVALID_HTML")
            pairs = dict(attrs)
            _need(len(pairs) == len(attrs), "INVALID_HTML")
            self.current, self.size = [], 0
            self.eligible = "src" not in pairs and pairs.get("type", "") in {
                "", "text/javascript", "application/javascript",
            }

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.size += len(data.encode("utf-8"))
            _need(self.size <= MAX_SCRIPT_BYTES, "SCRIPT_TOO_LARGE")
            if self.eligible:
                self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.current is not None:
            if self.eligible:
                self.blocks.append("".join(self.current))
                _need(len(self.blocks) <= MAX_SCRIPTS, "TOO_MANY_SCRIPTS")
            self.current = None


def _spaces(text: str, offset: int) -> int:
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
        elif text.startswith(("//", "<!--", "-->"), offset):
            end = text.find("\n", offset + 2)
            offset = len(text) if end < 0 else end + 1
        elif text.startswith("/*", offset):
            end = text.find("*/", offset + 2)
            _need(end >= 0, "INVALID_JAVASCRIPT_COMMENT")
            offset = end + 2
        else:
            break
    return offset


def _quoted(text: str, offset: int) -> int:
    quote, offset = text[offset], offset + 1
    while offset < len(text):
        if text[offset] == "\\":
            offset += 2
        elif text[offset] == quote:
            return offset + 1
        else:
            offset += 1
    raise DtekDataError("INVALID_JAVASCRIPT_STRING")


def _json_object_end(text: str, start: int) -> int:
    stack: list[str] = []
    offset = start
    while offset < len(text):
        char = text[offset]
        if char == '"':
            offset = _quoted(text, offset)
            continue
        if char in "{[":
            stack.append(char)
            _need(len(stack) <= MAX_JSON_DEPTH, "JSON_TOO_DEEP")
        elif char in "}]":
            _need(bool(stack) and stack.pop() == ("{" if char == "}" else "["), "INVALID_JSON")
            if not stack:
                return offset + 1
        offset += 1
    raise DtekDataError("INVALID_JSON")


def _regex_end(text: str, start: int) -> int:
    """Skip regex literals in expression-start contexts; never evaluate them."""
    offset, bracket = start + 1, False
    while offset < len(text) and text[offset] not in "\r\n":
        char = text[offset]
        if char == "\\":
            offset += 2
            continue
        if char == "[":
            bracket = True
        elif char == "]":
            bracket = False
        elif char == "/" and not bracket:
            offset += 1
            while offset < len(text) and text[offset].isascii() and text[offset].isalpha():
                offset += 1
            return offset
        offset += 1
    raise DtekDataError("INVALID_JAVASCRIPT_REGEX")


def _assignments(script: str) -> list[str]:
    offset = 0
    tokens: deque[str] = deque(maxlen=4)
    found: list[str] = []
    while offset < len(script):
        offset = _spaces(script, offset)
        if offset == len(script):
            break
        char = script[offset]
        if char in "\"'`":
            offset = _quoted(script, offset)
            tokens.append("STRING")
            continue
        if char == "/" and (not tokens or tokens[-1] in {
            "=", "(", "[", ",", ":", "!", "?", "return", ";", "{",
        }):
            offset = _regex_end(script, offset)
            tokens.append("REGEX")
            continue
        match = _IDENTIFIER.match(script, offset)
        if match is None:
            tokens.append(char)
            offset += 1
            continue
        token = match.group(0)
        end = offset + len(token)
        bare = not tokens or tokens[-1] in {";", "{", "}", "var", "let", "const"}
        member = len(tokens) >= 2 and list(tokens)[-2:] in (
            ["DisconSchedule", "."], ["window", "."],
        ) and (len(tokens) < 3 or tokens[-3] != ".")
        next_pos = _spaces(script, end)
        if token == "fact" and (bare or member) and script[next_pos:next_pos + 1] == "=":
            _need(script[next_pos:next_pos + 2] not in {"==", "=>"}, "INVALID_FACT_ASSIGNMENT")
            start = _spaces(script, next_pos + 1)
            _need(script[start:start + 1] == "{", "NON_LITERAL_FACT")
            final = _json_object_end(script, start)
            tail = _spaces(script, final)
            _need(tail == len(script) or script[tail] == ";", "TRAILING_FACT_JAVASCRIPT")
            found.append(script[start:final])
            offset = final
            tokens.append("OBJECT")
        else:
            tokens.append(token)
            offset = end
    return found


def _pairs(rows: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in rows:
        _need(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise DtekDataError("NONFINITE_JSON")


def extract_fact_object(html: str | bytes) -> dict[str, object]:
    try:
        if type(html) is bytes:
            _need(len(html) <= MAX_HTML_BYTES, "HTML_TOO_LARGE")
            html = html.decode("utf-8", "strict")
        _need(type(html) is str, "INVALID_HTML")
        text = cast(str, html)
        _need(len(text.encode("utf-8")) <= MAX_HTML_BYTES, "HTML_TOO_LARGE")
        parser = _Scripts()
        parser.feed(text)
        parser.close()
        _need(parser.current is None, "INVALID_HTML")
        found = [raw for script in parser.blocks for raw in _assignments(script)]
        _need(len(found) == 1, "FACT_NOT_FOUND" if not found else "AMBIGUOUS_FACT")
        value: object = json.loads(found[0], object_pairs_hook=_pairs, parse_constant=_constant)
        pending, nodes = [value], 0
        while pending:
            item = pending.pop()
            nodes += 1
            _need(nodes <= MAX_JSON_NODES, "TOO_MANY_JSON_NODES")
            if type(item) is dict:
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
        _need(type(value) is dict, "INVALID_FACT_OBJECT")
        return cast(dict[str, object], value)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise DtekDataError("INVALID_FACT_ENCODING_OR_JSON") from exc


def _epoch(value: object) -> tuple[int, date]:
    if type(value) is str:
        _need(bool(re.fullmatch(r"[1-9][0-9]{8,9}", value)), "INVALID_FACT_DATE")
        value = int(value)
    _need(type(value) is int and 946684800 <= value <= 4102444800, "INVALID_FACT_DATE")
    number = cast(int, value)
    stamp = datetime.fromtimestamp(number, UTC).astimezone(KYIV)
    _need(stamp.hour == stamp.minute == stamp.second == 0, "NON_MIDNIGHT_FACT_DATE")
    lower, upper = day_bounds(stamp.date())
    _need(upper - lower == timedelta(hours=24), "AMBIGUOUS_24_HOUR_DAY")
    return number, stamp.date()


def _updated(value: object) -> datetime:
    if type(value) is int:
        _need(946684800 <= value <= 4102444800, "INVALID_FACT_UPDATE")
        return datetime.fromtimestamp(value, UTC)
    _need(type(value) is str and 1 <= len(value) <= 40, "INVALID_FACT_UPDATE")
    text = cast(str, value)
    matched = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2})(?::(\d{2}))?", text)
    try:
        if matched:
            day = datetime.strptime(matched[1], "%d.%m.%Y").date()
            seconds = int(matched[3] or "0")
            _need(seconds <= 59, "INVALID_FACT_UPDATE")
            return local_boundary(day, matched[2]) + timedelta(seconds=seconds)
        parsed = datetime.fromisoformat(text)
        _need(parsed.tzinfo is not None and parsed.utcoffset() is not None, "INVALID_FACT_UPDATE")
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise DtekDataError("INVALID_FACT_UPDATE") from exc


def parse_dtek_fact(
    html: str | bytes, *, mapping_version: str = REFERENCE_MAPPING_VERSION
) -> DtekFact:
    _need(mapping_version == REFERENCE_MAPPING_VERSION, "UNSUPPORTED_FACT_MAPPING")
    fact = extract_fact_object(html)
    _need(set(fact) == {"data", "today", "update"}, "INVALID_FACT_SCHEMA")
    today_epoch, today = _epoch(fact["today"])
    _need(type(fact["data"]) is dict, "INVALID_FACT_DATA")
    data = cast(dict[str, object], fact["data"])
    _need(1 <= len(data) <= 7, "INVALID_FACT_DATA")
    _need(str(today_epoch) in data, "MISSING_TODAY_FACT_DATE")
    days: list[DtekRawDay] = []
    for raw_day, raw_groups in sorted(data.items()):
        _, day = _epoch(raw_day)
        _need(today <= day <= today + timedelta(days=6), "OUT_OF_RANGE_FACT_DATE")
        _need(type(raw_groups) is dict, "INCOMPLETE_FACT_GROUPS")
        groups = cast(dict[str, object], raw_groups)
        _need(set(groups) == set(EXTERNAL_GROUPS), "INCOMPLETE_FACT_GROUPS")
        normalized: list[DtekRawGroup] = []
        for group_id in GROUPS:
            raw_hours = groups["GPV" + group_id]
            _need(type(raw_hours) is dict, "INCOMPLETE_FACT_HOURS")
            hours = cast(dict[str, object], raw_hours)
            _need(set(hours) == {
                str(hour) for hour in range(1, 25)
            }, "INCOMPLETE_FACT_HOURS")
            values = tuple(hours[str(hour)] for hour in range(1, 25))
            _need(all(type(value) is str and bool(re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]{0,31}", value
            )) for value in values), "INVALID_FACT_STATE")
            normalized.append(DtekRawGroup(group_id, cast(tuple[str, ...], values)))
        days.append(DtekRawDay(day, tuple(normalized)))
    return DtekFact(today, _updated(fact["update"]), tuple(days))
