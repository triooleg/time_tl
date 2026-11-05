from copy import deepcopy

import pytest

from tests.infrastructure.test_weather_notification_repository import (
    NOW,
    USER,
    Session,
    daily_claim,
)
from tests.infrastructure.test_weather_notification_repository import (
    store as store,
)
from time_tl.infrastructure.db.models import OutboxMessage, RuntimeSetting
from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner


@pytest.mark.parametrize("daily", [False, True])
@pytest.mark.parametrize("state", ["dispatch", "indeterminate", "attempted", "claimed"])
async def test_ambiguous_identity_cannot_resend_after_preference_and_config_epoch_change(
    store, daily, state
):
    if daily:
        daily_claim(store)
    row = store.row
    store.rows[OutboxMessage] = [row]
    row.claimed_at, row.dispatch_started_at, row.indeterminate_at = None, None, None
    row.dead_at, row.attempts = NOW, 0
    if state == "dispatch":
        row.dispatch_started_at = NOW
    elif state == "indeterminate":
        row.indeterminate_at = NOW
    elif state == "attempted":
        row.attempts = 1
    else:
        row.claimed_at = NOW
    old_payload = deepcopy(row.payload)
    store.pref.revision += 1
    store.rows[RuntimeSetting][0].value["revision"] += 1
    assert await PostgresWeatherNotificationPlanner(store, enabled=True).plan_user(USER, NOW) == 0
    assert store.rows[OutboxMessage] == [row] and row.payload == old_payload


async def test_new_claim_also_checks_semantic_ambiguity_at_final_dispatch(store):
    from uuid import uuid4

    from time_tl.infrastructure.db import weather_notifications as db
    from time_tl.infrastructure.db.models import WeatherNotificationDelivery

    prior = OutboxMessage(
        id=uuid4(),
        event_key="weather:" + "d" * 64,
        event_type=store.row.event_type,
        aggregate_type="weather",
        aggregate_id=str(USER),
        payload=deepcopy(store.row.payload),
        dispatch_started_at=NOW,
        indeterminate_at=NOW,
        dead_at=NOW,
        attempts=1,
    )
    store.rows[OutboxMessage] = [store.row, prior]
    store.rows[WeatherNotificationDelivery].append(
        WeatherNotificationDelivery(
            user_id=USER,
            notification_key="d" * 64,
            outbox_id=prior.id,
            event_keys=list(prior.payload["event_keys"]),
            created_at=NOW,
        )
    )
    assert not await db.weather_delivery_policy(Session(store), store.row, NOW)
