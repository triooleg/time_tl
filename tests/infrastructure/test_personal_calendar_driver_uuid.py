"""Real asyncpg UUID values, without a database or synthetic driver substitute."""
from dataclasses import replace
from datetime import date, time
from uuid import UUID

import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID

from time_tl.domain.calendar_overrides import (
    OverrideAction,
    PersonalOverride,
    event_choices,
)
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile

DAY = date(2026, 10, 4)
ID = UUID("70f2fdb8-e283-4424-b1dd-b6308da5804c")
PROFILE = ScheduleProfile(
    "synthetic", "Synthetic",
    overrides=(DateOverride(DAY, lessons=(Lesson(time(9), time(10), "Synthetic"),)),),
)


def test_real_asyncpg_uuid_preserves_event_identity_and_typed_changes():
    driver_id = DriverUUID(str(ID))
    assert isinstance(driver_id, UUID) and type(driver_id) is not UUID
    standard = event_choices(ID, 1, 1, PROFILE, DAY, user_id=2)
    native = event_choices(driver_id, 1, 1, PROFILE, DAY, user_id=2)
    assert len(native) == 1 and native == standard
    choice = replace(native[0], event_id=DriverUUID(str(native[0].event_id)))
    stored = PersonalOverride(
        user_id=2, calendar_id=driver_id, snapshot_version=1,
        source_day=DAY, event_id=choice.event_id, action=OverrideAction.CANCEL, version=1,
    )
    assert stored.calendar_id == ID and stored.event_id == standard[0].event_id


@pytest.mark.parametrize("value", [None, False, True, 1, str(ID), ID.bytes, {}, []])
def test_non_uuid_values_still_rejected(value):
    with pytest.raises(ValueError):
        event_choices(value, 1, 1, PROFILE, DAY, user_id=2)
