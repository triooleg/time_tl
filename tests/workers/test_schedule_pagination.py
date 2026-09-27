from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from time_tl.application.schedule_worker import (
    ConfirmedClear,
    ConfirmedClearPage,
    ScheduleTarget,
    ScheduleWorker,
)
from time_tl.domain.alerts import AlertState, NotificationMode
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule


class PagedRepository:
    def __init__(self, targets: tuple[ScheduleTarget, ...]) -> None:
        self.targets = targets
        self.calls: list[tuple[str | None, int]] = []
        self.batch_sizes: list[int] = []
        self.event_keys: set[str] = set()

    async def list_schedule_targets_page(self, now, *, after_user_id, limit):
        self.calls.append((after_user_id, limit))
        start = 0
        if after_user_id is not None:
            start = next(
                index + 1
                for index, target in enumerate(self.targets)
                if target.user_id == after_user_id
            )
        return self.targets[start : start + limit]

    async def list_schedule_targets(self, now):
        raise AssertionError("production pagination must be preferred")

    async def list_confirmed_clears(self, now):
        return ()

    async def enqueue_deduplicated(self, messages):
        self.batch_sizes.append(len(messages))
        inserted = 0
        for message in messages:
            if message.event_key not in self.event_keys:
                self.event_keys.add(message.event_key)
                inserted += 1
        return inserted


def make_target(index: int) -> ScheduleTarget:
    profile = ScheduleProfile(
        f"profile-{index}",
        "Розклад",
        rules=(
            ScheduleRule(
                0,
                (Lesson(time(9), time(9, 45), "Математика"),),
            ),
        ),
        active=True,
    )
    return ScheduleTarget(
        f"u{index:04d}",
        10_000 + index,
        profile,
        NotificationMode.LESSONS_ONLY,
        5,
        AlertState.UNKNOWN,
    )


@pytest.mark.asyncio
async def test_schedule_scan_is_cursor_paged_complete_and_deduplicated() -> None:
    repository = PagedRepository(tuple(make_target(index) for index in range(205)))
    worker = ScheduleWorker(repository)
    now = datetime(2026, 9, 21, 5, 56, tzinfo=UTC)

    assert await worker.run_once(now) == 205
    assert [cursor for cursor, _limit in repository.calls] == [
        None,
        "u0099",
        "u0199",
        "u0204",
    ]
    assert repository.batch_sizes == [100, 100, 5]
    assert len(repository.event_keys) == 205

    repository.calls.clear()
    repository.batch_sizes.clear()
    assert await worker.run_once(now) == 0
    assert repository.batch_sizes == [100, 100, 5]
    assert len(repository.event_keys) == 205


class PagedClearRepository(PagedRepository):
    def __init__(self, clears: tuple[ConfirmedClear, ...], confirmed_at: datetime) -> None:
        super().__init__(())
        self.clears = clears
        self.confirmed_at = confirmed_at
        self.clear_calls: list[str | None] = []

    async def list_schedule_targets_page(self, now, *, after_user_id, limit):
        return ()

    async def list_confirmed_clears_page(
        self,
        now,
        *,
        after_confirmed_at,
        after_message_id,
        limit,
    ):
        self.clear_calls.append(after_message_id)
        start = int(after_message_id[1:]) + 1 if after_message_id else 0
        page = self.clears[start : start + limit]
        if not page:
            return ConfirmedClearPage((), None, None)
        last_index = start + len(page) - 1
        return ConfirmedClearPage(
            page,
            self.confirmed_at,
            f"m{last_index:04d}",
        )

    async def list_confirmed_clears(self, now):
        raise AssertionError("production clear pagination must be preferred")


@pytest.mark.asyncio
async def test_confirmed_clear_fanout_is_cursor_paged_and_bounded() -> None:
    confirmed_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
    clears = tuple(
        ConfirmedClear(
            f"clear-{index}",
            confirmed_at,
            ScheduleTarget(
                f"u{index:04d}",
                20_000 + index,
                ScheduleProfile(
                    f"profile-{index}",
                    "Розклад",
                    rules=(
                        ScheduleRule(
                            0,
                            (Lesson(time(9), time(9, 45), "Математика"),),
                        ),
                    ),
                    active=True,
                ),
                NotificationMode.LESSONS_AND_ALERTS,
                5,
                AlertState.CLEAR_CONFIRMED,
            ),
        )
        for index in range(205)
    )
    repository = PagedClearRepository(clears, confirmed_at)

    assert await ScheduleWorker(repository).run_once(confirmed_at) == 205
    assert repository.clear_calls == [None, "m0099", "m0199", "m0204"]
    assert repository.batch_sizes == [100, 100, 5]
    assert len(repository.event_keys) == 205
