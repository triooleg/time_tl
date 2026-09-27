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
REGIONS = frozenset({"community", "district", "region"})


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


def active(alert_id=10):
    return {
        "alerts": [
            {
                "id": alert_id,
                "location_uid": "community",
                "alert_type": "air_raid",
                "started_at": "2026-09-20T10:00:00Z",
                "finished_at": None,
            }
        ]
    }


def adapter(transport):
    return AlertsInUaAdapter(
        token="secret-not-logged", transport=transport, covered_region_ids=REGIONS, now=lambda: NOW
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
            response(200, active(), **{"Last-Modified": "marker"}),
            response(304),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)
        result = await provider.fetch(fetch_revision=2, last_modified="marker")
        self.assertIsNotNone(first.snapshot)
        self.assertEqual("marker", transport.requests[1].headers["If-Modified-Since"])
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
        result = await adapter(Transport(response(200, payload, **{"Last-Modified": "m"}))).fetch(
            fetch_revision=1, last_modified=None
        )
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
                    Transport(response(200, sample, **{"Last-Modified": "x"}))
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
        self.scope = RegionScope.from_hierarchy("community", ancestors=("district", "region"))
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
        await self.worker(Transport(response(200, active(), **{"Last-Modified": "m1"}))).run_once()
        self.clock += 15
        await self.worker(Transport(response(304))).run_once()
        snapshot = await self.repo.snapshot_for("student-1")
        self.assertEqual(AlertState.STALE, snapshot.state)
        self.assertEqual(1, snapshot.last_fetch_revision)
        self.assertEqual(1, len(await self.repo.outbox()))

    async def test_failure_after_active_is_stale_never_clear(self):
        await self.worker(Transport(response(200, active(), **{"Last-Modified": "m1"}))).run_once()
        self.clock += 15
        await self.worker(Transport(OSError("offline"))).run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)
        self.assertFalse(
            any(
                x.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for x in await self.repo.outbox()
            )
        )

    async def test_401_opens_persisted_circuit_until_operator_resume(self):
        await self.worker(Transport(response(401))).run_once()
        self.assertTrue((await self.repo.poll_state()).circuit_open)
        self.clock += 30
        with self.assertRaises(OperatorCircuitOpen):
            await self.worker(
                Transport(response(200, active(), **{"Last-Modified": "m"}))
            ).run_once()
        await self.repo.resume_operator_circuit()
        self.assertFalse((await self.repo.poll_state()).circuit_open)

    async def test_429_backoff_prevents_transport_call(self):
        await self.worker(Transport(response(429, **{"Retry-After": "45"}))).run_once()
        self.clock += 15
        transport = Transport(response(200, active(), **{"Last-Modified": "m"}))
        delay = await self.worker(transport).run_once()
        self.assertEqual([], transport.requests)
        self.assertGreaterEqual(delay, 30)

    async def test_production_loop_applies_jitter_and_holds_singleton_lease(self):
        transport = Transport(response(200, active(), **{"Last-Modified": "m"}))
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
        transport = Transport(response(200, active(), **{"Last-Modified": "m"}))
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
