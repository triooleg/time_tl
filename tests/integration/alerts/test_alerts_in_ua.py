import json
import unittest
from datetime import UTC, datetime

from time_tl.application.alert_worker import AlertWorker
from time_tl.domain.alerts import AlertState, RegionScope, TransitionKind
from time_tl.infrastructure.alerts import (
    AlertsInUaAdapter,
    HttpResponse,
    InMemoryAlertRepository,
    InMemorySingletonLease,
    NoCatchUpRateLimiter,
    OperatorCircuitOpen,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
MARKER_0 = "Sun, 20 Sep 2026 09:00:00 GMT"
MARKER_1 = "Sun, 20 Sep 2026 10:00:00 GMT"
MARKER_2 = "Sun, 20 Sep 2026 11:00:00 GMT"
REGION_TYPES = {"1903": "Громада", "1803": "Район", "16": "Область"}


class Transport:
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def send(self, request):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def response(status, body=None, **headers):
    return HttpResponse(status, headers, b"" if body is None else json.dumps(body).encode())


def active(
    alert_id=10,
    *,
    uid="1903",
    location_type="hromada",
    alert_type="air_raid",
    alert_level="red",
):
    return {
        "alerts": [
            {
                "id": alert_id,
                "location_title": "Тестова територіальна громада",
                "location_title_en": "Test territorial community",
                "location_type": location_type,
                "started_at": "2026-09-20T10:00:00Z",
                "finished_at": None,
                "updated_at": "2026-09-20T10:01:00+00:00",
                "alert_type": alert_type,
                "location_uid": uid,
                "location_oblast": "Луганська область",
                "location_oblast_uid": "16",
                "location_raion": "Алчевський район",
                "notes": "",
                "alert_level": alert_level,
            }
        ]
    }


def adapter(transport):
    return AlertsInUaAdapter(
        token="secret-not-logged",
        transport=transport,
        region_types=REGION_TYPES,
        now=lambda: NOW,
    )


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_200_uses_bearer_and_strict_full_mapping(self):
        transport = Transport(
            response(200, active(), **{"Last-Modified": "Sun, 20 Sep 2026 12:00:00 GMT"})
        )
        result = await adapter(transport).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.snapshot)
        self.assertEqual("Bearer secret-not-logged", transport.requests[0].headers["Authorization"])
        self.assertNotIn("token", transport.requests[0].url)
        self.assertEqual("10", result.snapshot.active_episodes[0].episode_id)

    async def test_304_is_used_only_after_same_process_validated_200(self):
        transport = Transport(
            response(200, active(), **{"Last-Modified": MARKER_1}),
            response(304),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)
        result = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        self.assertIsNotNone(first.snapshot)
        self.assertEqual(MARKER_1, transport.requests[1].headers["If-Modified-Since"])
        self.assertEqual("NOT_MODIFIED_304", result.snapshot.kind.value)

    async def test_304_without_baseline_is_invalid(self):
        result = await adapter(Transport(response(304))).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.failure)

    async def test_restart_does_not_trust_persisted_conditional_marker(self):
        transport = Transport(response(304))
        result = await adapter(transport).fetch(fetch_revision=2, last_modified="persisted")
        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNotNone(result.failure)

    async def test_unknown_provider_uid_invalidates_whole_snapshot(self):
        payload = active()
        payload["alerts"][0]["location_uid"] = "not-in-reviewed-catalogue"
        result = await adapter(
            Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.failure)

    async def test_empty_validated_list_is_a_complete_snapshot(self):
        result = await adapter(
            Transport(response(200, {"alerts": []}, **{"Last-Modified": MARKER_1}))
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual((), result.snapshot.active_episodes)
        self.assertEqual(REGION_TYPES.keys(), result.snapshot.covered_region_ids)

    async def test_unknown_uid_invalidates_even_a_non_air_alert(self):
        payload = active(uid="999", location_type="hromada", alert_type="chemical")
        result = await adapter(
            Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.failure)

    async def test_location_type_must_match_pinned_provider_catalogue(self):
        result = await adapter(
            Transport(
                response(
                    200,
                    active(uid="1803", location_type="hromada"),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.failure)

    async def test_red_and_yellow_air_raid_are_both_active(self):
        for level in ("red", "yellow"):
            with self.subTest(level=level):
                result = await adapter(
                    Transport(
                        response(
                            200,
                            active(alert_level=level),
                            **{"Last-Modified": MARKER_1},
                        )
                    )
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNotNone(result.snapshot)
                assert result.snapshot is not None
                self.assertEqual(1, len(result.snapshot.active_episodes))

    async def test_known_non_air_alert_is_validated_then_excluded(self):
        result = await adapter(
            Transport(
                response(
                    200,
                    active(alert_type="artillery_shelling"),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual((), result.snapshot.active_episodes)

    async def test_schema_enums_timestamps_and_duplicate_ids_fail_closed(self):
        samples = []

        def changed(**values):
            payload = active()
            payload["alerts"][0].update(values)
            return payload

        samples.extend(
            (
                changed(id=True),
                changed(location_uid="01903"),
                changed(alert_type="new_alert_kind"),
                changed(alert_level="green"),
                changed(location_type="unknown"),
                changed(started_at="2026-09-20T10:00:00"),
                changed(updated_at="not-a-timestamp"),
                changed(undocumented="schema-drift"),
            )
        )
        duplicate = active()
        duplicate["alerts"].append(dict(duplicate["alerts"][0]))
        samples.append(duplicate)
        bad_threat = active()
        bad_threat["alerts"][0]["threats"] = [
            {
                "threat_type": "not-documented",
                "level": "yellow",
                "started_at": "2026-09-20T10:00:01Z",
            }
        ]
        samples.append(bad_threat)

        for sample in samples:
            with self.subTest(sample=sample):
                result = await adapter(
                    Transport(response(200, sample, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNotNone(result.failure)

    async def test_marker_regression_and_same_marker_changed_content_are_invalid(self):
        regression_provider = adapter(
            Transport(
                response(200, active(), **{"Last-Modified": MARKER_2}),
                response(200, active(), **{"Last-Modified": MARKER_1}),
            )
        )
        first = await regression_provider.fetch(fetch_revision=1, last_modified=None)
        second = await regression_provider.fetch(fetch_revision=2, last_modified=MARKER_2)
        self.assertIsNotNone(first.snapshot)
        self.assertIsNotNone(second.failure)

        changed_provider = adapter(
            Transport(
                response(200, active(10), **{"Last-Modified": MARKER_1}),
                response(200, active(11), **{"Last-Modified": MARKER_1}),
            )
        )
        await changed_provider.fetch(fetch_revision=1, last_modified=None)
        changed = await changed_provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        self.assertIsNotNone(changed.failure)

    async def test_304_marker_must_match_validated_baseline(self):
        provider = adapter(
            Transport(
                response(200, active(), **{"Last-Modified": MARKER_1}),
                response(304, **{"Last-Modified": MARKER_2}),
            )
        )
        await provider.fetch(fetch_revision=1, last_modified=None)
        result = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        self.assertIsNotNone(result.failure)

    async def test_restart_first_200_is_unconditional_and_respects_persisted_floor(self):
        transport = Transport(response(200, active(), **{"Last-Modified": MARKER_0}))
        result = await adapter(transport).fetch(fetch_revision=2, last_modified=MARKER_1)
        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNotNone(result.failure)

    async def test_empty_malformed_and_http_failures_are_not_snapshots(self):
        cases = (
            Transport(HttpResponse(200, {"Last-Modified": MARKER_1}, b"")),
            Transport(HttpResponse(200, {"Last-Modified": MARKER_1}, b"not-json")),
            Transport(response(500, {"alerts": []})),
        )
        for transport in cases:
            result = await adapter(transport).fetch(fetch_revision=1, last_modified=None)
            self.assertIsNotNone(result.failure)

    async def test_invalid_partial_or_finished_payload_fails_closed(self):
        samples = [
            {"alerts": [{"id": 1}]},
            {
                "alerts": [
                    {
                        "id": 1,
                        "location_uid": "community",
                        "alert_type": "air_raid",
                        "started_at": "x",
                        "finished_at": "done",
                    }
                ]
            },
            {"alerts": [], "partial": True},
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                result = await adapter(
                    Transport(response(200, sample, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNotNone(result.failure)

    async def test_429_retry_after_and_401_operator_circuit(self):
        limited = await adapter(Transport(response(429, **{"Retry-After": "45"}))).fetch(
            fetch_revision=1, last_modified=None
        )
        denied = await adapter(Transport(response(401))).fetch(fetch_revision=2, last_modified=None)
        self.assertEqual(45, limited.retry_after_seconds)
        self.assertTrue(denied.open_operator_circuit)

    async def test_network_error_is_failure(self):
        result = await adapter(Transport(OSError("offline"))).fetch(
            fetch_revision=1, last_modified=None
        )
        self.assertIsNotNone(result.failure)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = RegionScope.from_hierarchy("1903", ancestors=("1803", "16"))
        self.repo = InMemoryAlertRepository([("student-1", self.scope)])
        self.clock = 100.0

    def worker(self, transport):
        return AlertWorker(
            provider=adapter(transport),
            repository=self.repo,
            lease=InMemorySingletonLease(),
            monotonic=lambda: self.clock,
            jitter=lambda a, b: 7.0,
        )

    async def test_restart_rejects_unverifiable_304_without_false_clear(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        self.clock += 15
        await self.worker(Transport(response(304))).run_once()
        snapshot = await self.repo.snapshot_for("student-1")
        self.assertEqual(AlertState.STALE, snapshot.state)
        self.assertEqual(1, snapshot.last_fetch_revision)
        self.assertEqual(1, len(await self.repo.outbox()))

    async def test_failure_after_active_is_stale_never_clear(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        self.clock += 15
        await self.worker(Transport(OSError("offline"))).run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)
        self.assertFalse(
            any(
                x.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for x in await self.repo.outbox()
            )
        )

    async def test_unknown_uid_after_active_is_stale_never_clear(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        self.clock += 15
        unknown = active(uid="999")
        await self.worker(
            Transport(response(200, unknown, **{"Last-Modified": MARKER_2}))
        ).run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_two_successful_empty_observations_are_required_for_clear(self):
        transport = Transport(
            response(200, active(), **{"Last-Modified": MARKER_0}),
            response(200, {"alerts": []}, **{"Last-Modified": MARKER_1}),
            response(200, {"alerts": []}, **{"Last-Modified": MARKER_2}),
        )
        worker = self.worker(transport)
        await worker.run_once()
        self.clock += 15
        await worker.run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)
        self.clock += 15
        await worker.run_once()
        self.assertEqual(
            AlertState.CLEAR_CONFIRMED,
            (await self.repo.snapshot_for("student-1")).state,
        )

    async def test_401_opens_persisted_circuit_until_operator_resume(self):
        await self.worker(Transport(response(401))).run_once()
        self.assertTrue((await self.repo.poll_state()).circuit_open)
        self.clock += 30
        with self.assertRaises(OperatorCircuitOpen):
            await self.worker(
                Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
            ).run_once()
        await self.repo.resume_operator_circuit()
        self.assertFalse((await self.repo.poll_state()).circuit_open)

    async def test_429_backoff_prevents_transport_call(self):
        await self.worker(Transport(response(429, **{"Retry-After": "45"}))).run_once()
        self.clock += 15
        transport = Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        delay = await self.worker(transport).run_once()
        self.assertEqual([], transport.requests)
        self.assertGreaterEqual(delay, 30)

    async def test_production_loop_applies_jitter_and_holds_singleton_lease(self):
        transport = Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        worker = self.worker(transport)
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise RuntimeError("stop test loop")

        with self.assertRaises(RuntimeError):
            await worker.serve(sleep)
        self.assertEqual(7, sleeps[0])
        self.assertEqual(15, sleeps[1])
        self.assertEqual(1, len(transport.requests))

    async def test_production_loop_degrades_restart_before_jitter_or_provider(self):
        transport = Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        worker = self.worker(transport)
        events = []
        original = self.repo.apply_process_restart_atomically

        async def restart():
            events.append("restart")
            await original()

        async def sleep(_delay):
            events.append("sleep")
            raise RuntimeError("stop before provider")

        self.repo.apply_process_restart_atomically = restart
        with self.assertRaises(RuntimeError):
            await worker.serve(sleep)
        self.assertEqual(["restart", "sleep"], events)
        self.assertEqual([], transport.requests)

    async def test_repeated_failures_use_persisted_exponential_backoff(self):
        await self.worker(Transport(OSError("offline"))).run_once()
        self.assertEqual(15, (await self.repo.poll_state()).retry_not_before_monotonic - self.clock)
        self.clock += 15
        await self.worker(Transport(OSError("offline"))).run_once()
        state = await self.repo.poll_state()
        self.assertEqual(2, state.consecutive_failures)
        self.assertEqual(30, state.retry_not_before_monotonic - self.clock)

    def test_limits_and_jitter_contract(self):
        limiter = NoCatchUpRateLimiter(5)
        self.assertTrue(limiter.consume(0))
        self.assertFalse(limiter.consume(11))
        self.assertTrue(limiter.consume(120))
        self.assertFalse(limiter.consume(120))
        self.assertEqual(7, self.worker(Transport()).startup_jitter_seconds)
        with self.assertRaises(ValueError):
            NoCatchUpRateLimiter(6)
        with self.assertRaises(ValueError):
            AlertWorker(
                provider=adapter(Transport()),
                repository=self.repo,
                lease=InMemorySingletonLease(),
                poll_interval_seconds=14,
                monotonic=lambda: 0,
            )


if __name__ == "__main__":
    unittest.main()
