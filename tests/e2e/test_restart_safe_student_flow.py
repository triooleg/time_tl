from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, time

from time_tl.application.alert_worker import AlertWorker
from time_tl.application.schedule_worker import (
    ConfirmedClear,
    PlannedOutboxMessage,
    ScheduleTarget,
    ScheduleWorker,
)
from time_tl.application.sender_worker import ClaimedOutboxMessage, SenderWorker
from time_tl.bot.models import NotificationMode as BotMode
from time_tl.bot.service import InMemoryAccountRepository, Region, TelegramApplication
from time_tl.domain.alerts import (
    AlertState,
    NotificationMode,
    RegionScope,
    TransitionKind,
)
from time_tl.domain.schedule import ScheduleProfile
from time_tl.infrastructure.alerts import (
    HttpResponse,
    InMemoryAlertRepository,
    InMemorySingletonLease,
    OblastAlertsInUaAdapter,
)
from time_tl.infrastructure.telegram import TelegramMessage


class ManualResolver:
    async def resolve(self, latitude: float, longitude: float) -> Region | None:
        del latitude, longitude
        return None


class ScriptedTransport:
    def __init__(self, item: HttpResponse | BaseException) -> None:
        self.item = item

    async def send(self, request):
        del request
        if isinstance(self.item, BaseException):
            raise self.item
        return self.item


def oblast_response(status: str, marker: str) -> HttpResponse:
    values = ["N"] * len(OblastAlertsInUaAdapter.OBLAST_UIDS)
    values[OblastAlertsInUaAdapter.OBLAST_UIDS.index("14")] = status
    return HttpResponse(200, {"Last-Modified": marker}, json.dumps("".join(values)).encode())


class DurableOutbox:
    """A shared instance models schedule/outbox state across worker restarts."""

    def __init__(self, target: ScheduleTarget) -> None:
        self.target = target
        self.clears: tuple[ConfirmedClear, ...] = ()
        self.records: dict[str, PlannedOutboxMessage] = {}
        self.delivered: set[str] = set()
        self.dead: set[str] = set()

    async def list_schedule_targets(self, now):
        del now
        return ()

    async def list_confirmed_clears(self, now):
        del now
        return self.clears

    async def enqueue_deduplicated(self, messages):
        created = 0
        for message in messages:
            if message.event_key not in self.records:
                self.records[message.event_key] = message
                created += 1
        return created

    async def claim_batch_skip_locked(self, now, *, limit):
        return tuple(
            ClaimedOutboxMessage(key, item.event_type, item.payload, 0)
            for key, item in self.records.items()
            if key not in self.delivered and key not in self.dead and item.available_at <= now
        )[:limit]

    async def deliver_if_authorized(self, message_id, delivered_at, deliver):
        del delivered_at
        record = self.records[message_id]
        authorized = (
            self.target.mode is NotificationMode.LESSONS_AND_ALERTS
            and self.target.alert_state is AlertState.CLEAR_CONFIRMED
            and record.payload.get("profile_revision") == self.target.profile_revision
            and message_id not in self.delivered
        )
        if not authorized:
            return False
        await deliver()
        self.delivered.add(message_id)
        return True

    async def mark_retry(self, message_id, *, available_at, error_code):
        del message_id, available_at, error_code

    async def mark_dead(self, message_id, *, error_code):
        del error_code
        self.dead.add(message_id)


class RecordingTelegram:
    def __init__(self) -> None:
        self.sent: list[TelegramMessage] = []

    async def send(self, message: TelegramMessage) -> None:
        self.sent.append(message)


class RestartSafeSimulationE2E(unittest.IsolatedAsyncioTestCase):
    async def test_onboarding_alert_restart_confirmed_clear_return_and_dedupe(self) -> None:
        accounts = InMemoryAccountRepository()
        app = TelegramApplication(
            regions=(Region("14", "Київська область"),),
            resolver=ManualResolver(),
            repository=accounts,
        )
        user_id, chat_id = 101, 50101
        app.choose_mode(user_id, BotMode.LESSONS_AND_SIRENS)
        app.propose_manual_region(user_id, "14")
        self.assertIsNone(accounts.accounts[user_id].region_id)
        app.confirm_region(user_id, confirmed=True)
        profile_id = app.create_profile(user_id, "8-А")
        app.add_lesson(
            user_id,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(9, 45),
            title="Алгебра",
        )
        account = accounts.accounts[user_id]
        self.assertEqual(account.region_id, "14")
        profile = account.profiles[profile_id]
        self.assertTrue(profile.active)

        alert_repo = InMemoryAlertRepository(
            [("student-101", RegionScope.from_hierarchy("14"))]
        )
        clock = 100.0

        async def poll(item: HttpResponse | BaseException) -> None:
            nonlocal clock
            provider = OblastAlertsInUaAdapter(
                token="simulation-token",
                transport=ScriptedTransport(item),
                now=lambda: datetime(2026, 9, 21, 6, 15, tzinfo=UTC),
            )
            # Recreate adapter and worker while retaining the durable repository.
            worker = AlertWorker(
                provider=provider,
                repository=alert_repo,
                lease=InMemorySingletonLease(),
                monotonic=lambda: clock,
                jitter=lambda _a, _b: 0,
            )
            await worker.run_once()
            clock += 300

        await poll(oblast_response("A", "m1"))
        snapshot = await alert_repo.snapshot_for("student-101")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, AlertState.ACTIVE)

        await poll(TimeoutError("simulated timeout"))
        snapshot = await alert_repo.snapshot_for("student-101")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, AlertState.STALE)
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await alert_repo.outbox()
            )
        )

        # P is active too and shares the stable oblast episode identity with A.
        await poll(oblast_response("P", "m2"))
        snapshot = await alert_repo.snapshot_for("student-101")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, AlertState.ACTIVE)

        # First N is only a candidate; the second fresh N confirms clear.
        await poll(oblast_response("N", "m3"))
        snapshot = await alert_repo.snapshot_for("student-101")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, AlertState.STALE)
        await poll(oblast_response("N", "m4"))
        snapshot = await alert_repo.snapshot_for("student-101")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, AlertState.CLEAR_CONFIRMED)
        alert_events = await alert_repo.outbox()
        self.assertEqual(
            [event.transition_kind for event in alert_events],
            [TransitionKind.ALERT_STARTED, TransitionKind.ALL_CLEAR_CONFIRMED],
        )

        lesson = profile.rules[0].lessons[0]
        lesson = replace(
            lesson,
            storage_ref="lesson:stable:algebra",
        )
        profile = ScheduleProfile(
            profile.profile_id,
            profile.name,
            rules=(replace(profile.rules[0], lessons=(lesson,)),),
            active=True,
        )
        target = ScheduleTarget(
            "student-101",
            chat_id,
            profile,
            NotificationMode.LESSONS_AND_ALERTS,
            alert_state=AlertState.CLEAR_CONFIRMED,
            profile_revision=1,
        )
        outbox = DurableOutbox(target)
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        outbox.clears = (ConfirmedClear(alert_events[-1].event_key, clear_at, target),)

        self.assertEqual(await ScheduleWorker(outbox).run_once(clear_at), 1)
        self.assertEqual(await ScheduleWorker(outbox).run_once(clear_at), 0)
        self.assertEqual(len(outbox.records), 1)

        telegram = RecordingTelegram()

        async def no_sleep(seconds: float) -> None:
            del seconds

        self.assertEqual(
            await SenderWorker(repository=outbox, transport=telegram).run_once(
                clear_at, sleep=no_sleep
            ),
            1,
        )
        self.assertEqual(
            await SenderWorker(repository=outbox, transport=telegram).run_once(
                clear_at, sleep=no_sleep
            ),
            0,
        )
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(telegram.sent[0].chat_id, chat_id)
        self.assertIn("підключайтеся", telegram.sent[0].text)


if __name__ == "__main__":
    unittest.main()
