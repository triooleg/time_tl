from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

import pytest

from time_tl.domain.calendar_memberships import (
    CALENDAR_SCHEMA_VERSION,
    MAX_CALENDAR_PAYLOAD_BYTES,
    MAX_INVITATION_HOURS,
    MAX_PERSISTENT_CALENDARS,
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarInvitationInvalid,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
    InvitationPreview,
    deserialize_profile,
    may_edit,
    may_manage,
    serialize_profile,
)
from time_tl.domain.calendars import (
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    ProjectionTimeError,
    build_my_day,
)
from time_tl.domain.schedule.models import (
    DateOverride,
    Lesson,
    Parity,
    ScheduleProfile,
    ScheduleRule,
)
from time_tl.domain.schedule.quotas import ScheduleValidationError
from time_tl.domain.schedule.resolver import ResolutionConflict, resolve_day

CALENDAR_ID = UUID("12345678-1234-4234-9234-123456789012")
DAY = date(2026, 9, 28)


def profile() -> ScheduleProfile:
    return ScheduleProfile(
        profile_id="private-old-identity",
        name="Private name",
        active=True,
        rules=(
            ScheduleRule(
                weekday=0,
                lessons=(
                    Lesson(
                        time(9),
                        time(9, 45),
                        "Math",
                        join_url="https://example.invalid/private",
                        storage_ref="private-old-storage-identity",
                    ),
                    Lesson(time(10), time(10, 45), "Math", enabled=False),
                ),
                valid_from=DAY,
                valid_to=date(2027, 1, 1),
            ),
        ),
        overrides=(DateOverride(date(2026, 9, 29), day_off=True),),
    )


def payload() -> dict[str, Any]:
    return serialize_profile(profile())


def decode(value: object) -> ScheduleProfile:
    return deserialize_profile(value, CALENDAR_ID, "Shared Math")


def test_codec_is_independent_link_free_versioned_and_inactive() -> None:
    original = profile()
    encoded = serialize_profile(original)
    dumped = json.dumps(encoded)
    for private in (original.profile_id, original.name, "private-old-storage", "https://"):
        assert private not in dumped
    assert set(encoded) == {"schema_version", "timezone", "rules", "overrides"}
    assert encoded["schema_version"] == CALENDAR_SCHEMA_VERSION
    restored = decode(json.loads(dumped))
    assert not restored.active
    assert restored.profile_id == str(CALENDAR_ID)
    assert restored.name == "Shared Math"
    assert restored.timezone == "Europe/Kyiv"
    assert restored.rules[0].lessons[0].join_url is None
    assert restored.rules[0].lessons[1].enabled is False
    encoded["rules"][0]["lessons"][0]["title"] = "changed"
    assert restored.rules[0].lessons[0].title == "Math"
    assert original.rules[0].lessons[0].join_url is not None


def test_occurrence_identity_survives_every_reload_and_repeated_titles() -> None:
    first = decode(payload())
    second = decode(payload())
    assert first.rules[0].lessons[0].storage_ref == second.rules[0].lessons[0].storage_ref
    assert first.rules[0].lessons[0].storage_ref != first.rules[0].lessons[1].storage_ref
    def sources(value: ScheduleProfile) -> tuple[CalendarSource, ...]:
        return (
            CalendarSource(
                Calendar(str(CALENDAR_ID), "Shared Math", CalendarKind.SCHOOL, value),
                CalendarMembership(1, str(CALENDAR_ID)),
            ),
        )
    instant = datetime(2026, 9, 28, 6, 5, tzinfo=UTC)
    assert build_my_day(1, sources(first), instant) == build_my_day(1, sources(second), instant)


@pytest.mark.parametrize("key", ["user_id", "name", "active", "region", "token", "join_url"])
def test_top_level_unknown_and_private_fields_reject(key: str) -> None:
    value = payload()
    value[key] = "private-secret"
    with pytest.raises(ValueError, match="unknown or missing") as error:
        decode(value)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("path", ["rule", "lesson", "override"])
@pytest.mark.parametrize("key", ["join_url", "storage_ref", "password", "telegram_id", "unknown"])
def test_nested_unknown_fields_and_even_empty_join_urls_reject(path: str, key: str) -> None:
    value = payload()
    target = {
        "rule": value["rules"][0],
        "lesson": value["rules"][0]["lessons"][0],
        "override": value["overrides"][0],
    }[path]
    target[key] = None
    with pytest.raises(ValueError, match="unknown or missing"):
        decode(value)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("weekday", True),
        ("weekday", "0"),
        ("weekday", 0.0),
        ("weekday", -1),
        ("weekday", 7),
        ("week_parity", None),
        ("week_parity", 1),
        ("week_parity", "private-secret"),
        ("date_parity", "odd"),
        ("valid_from", "20260928"),
        ("valid_from", True),
        ("valid_to", "2026-09-28T00:00:00"),
        ("lessons", ()),
    ],
)
def test_rule_types_are_not_coerced(field: str, bad: object) -> None:
    value = payload()
    value["rules"][0][field] = bad
    with pytest.raises(ValueError) as error:
        decode(value)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("start", "9:00"),
        ("start", "09:00:00"),
        ("start", "09:00+03:00"),
        ("end", "24:00"),
        ("end", "09:00"),
        ("title", 1),
        ("title", False),
        ("title", " "),
        ("enabled", 1),
        ("enabled", "false"),
    ],
)
def test_lesson_types_and_wall_times_are_strict(field: str, bad: object) -> None:
    value = payload()
    value["rules"][0]["lessons"][0][field] = bad
    with pytest.raises(ValueError):
        decode(value)


@pytest.mark.parametrize(
    "value",
    [None, [], "{}", {"schema_version": "1.0"}, float("nan"), object()],
)
def test_root_is_an_exact_json_object(value: object) -> None:
    with pytest.raises(ValueError):
        decode(value)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("schema_version", 1),
        ("schema_version", "2.0"),
        ("timezone", "UTC"),
        ("rules", ()),
        ("overrides", None),
    ],
)
def test_version_timezone_and_collection_types(field: str, bad: object) -> None:
    value = payload()
    value[field] = bad
    with pytest.raises(ValueError):
        decode(value)


def test_oversized_and_deeply_recursive_payloads_fail_boundedly() -> None:
    value = payload()
    value["rules"][0]["lessons"][0]["title"] = "x" * (MAX_CALENDAR_PAYLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="oversized"):
        decode(value)
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ValueError):
        decode(recursive)


@pytest.mark.parametrize("case", ["rules", "overrides", "lessons", "title", "total_lessons"])
def test_existing_schedule_quotas_are_authoritative(case: str) -> None:
    value = payload()
    if case == "rules":
        value["rules"] *= 33
    elif case == "overrides":
        value["overrides"] *= 91
    elif case == "lessons":
        value["rules"][0]["lessons"] = [value["rules"][0]["lessons"][1]] * 17
    elif case == "title":
        value["rules"][0]["lessons"][0]["title"] = "x" * 161
    else:
        rule = copy.deepcopy(value["rules"][0])
        rule["lessons"] = [rule["lessons"][1]] * 16
        value["rules"] = [rule] * 7
    with pytest.raises(ScheduleValidationError):
        decode(value)


def test_overlap_dates_duplicate_overrides_and_dayoff_consistency_reject() -> None:
    value = payload()
    value["rules"][0]["lessons"][1]["enabled"] = True
    value["rules"][0]["lessons"][1]["start"] = "09:30"
    with pytest.raises(ValueError, match="overlap"):
        decode(value)
    value = payload()
    value["overrides"] *= 2
    with pytest.raises(ValueError, match="one override"):
        decode(value)
    value = payload()
    value["overrides"][0]["lessons"] = value["rules"][0]["lessons"]
    with pytest.raises(ValueError, match="day-off"):
        decode(value)
    value = payload()
    value["rules"][0]["valid_to"] = "2026-09-27"
    with pytest.raises(ValueError, match="valid_from"):
        decode(value)


def test_parity_overrides_working_weekends_and_variable_breaks_survive() -> None:
    original = ScheduleProfile(
        "personal",
        "Name",
        rules=(
            ScheduleRule(
                5,
                (Lesson(time(9), time(9, 45), "Math"), Lesson(time(10, 15), time(11), "Art")),
                week_parity=Parity.EVEN,
                date_parity=Parity.ANY,
            ),
        ),
        overrides=(DateOverride(DAY, (Lesson(time(11), time(12), "Override"),)),),
    )
    restored = decode(serialize_profile(original))
    assert resolve_day(restored, date(2026, 10, 3)).lessons == original.rules[0].lessons
    assert resolve_day(restored, date(2026, 10, 3)).gaps[0].minutes == 30
    assert resolve_day(restored, DAY).source == "date_override"
    assert resolve_day(restored, date(2026, 10, 10)).lessons == ()
    first_ref = restored.overrides[0].lessons[0].storage_ref
    assert decode(serialize_profile(original)).overrides[0].lessons[0].storage_ref == first_ref


def test_equal_specificity_rule_conflicts_remain_fail_closed_in_resolver() -> None:
    value = payload()
    value["rules"] *= 2
    with pytest.raises(ResolutionConflict):
        resolve_day(decode(value), DAY)


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_codec_does_not_guess_ambiguous_or_nonexistent_kyiv_instants(day: date) -> None:
    original = ScheduleProfile(
        "legacy", "Name", overrides=(DateOverride(day, (Lesson(time(3, 15), time(3, 45), "DST"),)),)
    )
    restored = decode(serialize_profile(original))
    source = CalendarSource(
        Calendar(str(CALENDAR_ID), "Shared", CalendarKind.SCHOOL, restored),
        CalendarMembership(1, str(CALENDAR_ID)),
    )
    with pytest.raises(ProjectionTimeError):
        build_my_day(1, (source,), datetime.combine(day, time(10), tzinfo=UTC))


@pytest.mark.parametrize("role", list(CalendarRole))
def test_permission_helpers(role: CalendarRole) -> None:
    assert may_edit(role) is (role is not CalendarRole.MEMBER)
    assert may_manage(role) is (role is CalendarRole.OWNER)


@pytest.mark.parametrize("bad", ["OWNER", None, True, 1])
def test_permission_helpers_reject_coercion(bad: Any) -> None:
    with pytest.raises(ValueError):
        may_edit(bad)
    with pytest.raises(ValueError):
        may_manage(bad)


def summary() -> CalendarSummary:
    return CalendarSummary(CALENDAR_ID, "Name", CalendarKind.SCHOOL, CalendarRole.MEMBER, True, 1)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("calendar_id", str(CALENDAR_ID)),
        ("name", 1),
        ("name", " "),
        ("name", "x" * 121),
        ("kind", "SCHOOL"),
        ("role", "OWNER"),
        ("visible", 1),
        ("version", True),
        ("version", 0),
        ("version", "1"),
    ],
)
def test_summary_is_strict_bounded_and_immutable(field: str, bad: Any) -> None:
    with pytest.raises(ValueError):
        replace(summary(), **{field: bad})


def test_dtos_expose_no_payload_token_identity_or_private_preferences() -> None:
    item = summary()
    invitation = InvitationPreview(item, datetime(2026, 9, 28, tzinfo=UTC))
    assert "token" not in repr(invitation)
    assert "owner_user_id" not in repr(item)
    assert "payload" not in repr(item)
    with pytest.raises(FrozenInstanceError):
        item.visible = False  # type: ignore[misc]
    with pytest.raises(ValueError):
        InvitationPreview(item, datetime(2026, 9, 28))
    assert MAX_PERSISTENT_CALENDARS == 11
    assert MAX_INVITATION_HOURS == 24


@pytest.mark.parametrize(
    "error_type",
    [
        CalendarAccessDenied,
        CalendarConflict,
        CalendarLimitExceeded,
        CalendarDataUnavailable,
        CalendarInvitationInvalid,
    ],
)
def test_repository_errors_are_safe_generic_messages(error_type: type[ValueError]) -> None:
    assert str(error_type())
    assert "user_id" not in str(error_type())
    assert "token" not in str(error_type())


def test_codec_requires_external_uuid_identity_and_name() -> None:
    with pytest.raises(ValueError):
        deserialize_profile(payload(), str(CALENDAR_ID), "Name")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        deserialize_profile(payload(), CALENDAR_ID, " ")
