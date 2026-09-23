import json
import unittest
from datetime import UTC, datetime

from time_tl.domain.alerts import (
    AlertState,
    AlertStateMachine,
    ProviderSnapshotKind,
    RegionScope,
    SourceFailureKind,
    TransitionKind,
)
from time_tl.infrastructure.alerts import HttpResponse, OblastAlertsInUaAdapter

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


class Transport:
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def send(self, request):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def raw_response(status, body=b"", **headers):
    return HttpResponse(status, headers, body)


def json_response(status, value, **headers):
    return raw_response(status, json.dumps(value).encode(), **headers)


def adapter(transport):
    return OblastAlertsInUaAdapter(
        token="official-secret-not-logged",
        transport=transport,
        now=lambda: NOW,
    )


class OblastIotProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_and_p_are_active_n_is_absent_with_official_uid_order(self):
        statuses = "AP" + "N" * 25
        transport = Transport(
            json_response(200, statuses, **{"Last-Modified": "m1"})
        )

        result = await adapter(transport).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual(ProviderSnapshotKind.FULL_200, result.snapshot.kind)
        self.assertEqual(
            {("29", "air_raid", "oblast-air-raid:29"), ("8", "air_raid", "oblast-air-raid:8")},
            {
                (item.location_uid, item.alert_type, item.episode_id)
                for item in result.snapshot.active_episodes
            },
        )
        self.assertEqual(27, len(result.snapshot.covered_region_ids))
        request = transport.requests[0]
        self.assertEqual(OblastAlertsInUaAdapter.ENDPOINT, request.url)
        self.assertEqual("Bearer official-secret-not-logged", request.headers["Authorization"])
        self.assertNotIn("token", request.url)

    async def test_episode_identity_is_stable_when_partial_becomes_whole_oblast(self):
        transport = Transport(
            json_response(200, "P" + "N" * 26, **{"Last-Modified": "m1"}),
            json_response(200, "A" + "N" * 26, **{"Last-Modified": "m2"}),
        )
        provider = adapter(transport)
        partial = await provider.fetch(fetch_revision=1, last_modified=None)
        whole = await provider.fetch(fetch_revision=2, last_modified="m1")

        assert partial.snapshot is not None
        assert whole.snapshot is not None
        self.assertEqual(partial.snapshot.active_episodes, whole.snapshot.active_episodes)

    async def test_only_exact_json_string_of_27_known_statuses_is_accepted(self):
        samples = (
            b"",
            json.dumps("").encode(),
            json.dumps("N" * 26).encode(),
            json.dumps("N" * 28).encode(),
            json.dumps("N" * 26 + "X").encode(),
            json.dumps(["N"] * 27).encode(),
            json.dumps({"status": "N" * 27}).encode(),
        )
        for body in samples:
            with self.subTest(body=body):
                result = await adapter(
                    Transport(raw_response(200, body, **{"Last-Modified": "m"}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNotNone(result.failure)
                assert result.failure is not None
                self.assertEqual(SourceFailureKind.INVALID_RESPONSE, result.failure.kind)

    async def test_200_without_last_modified_is_partial_failure(self):
        result = await adapter(Transport(json_response(200, "N" * 27))).fetch(
            fetch_revision=1,
            last_modified=None,
        )
        self.assertIsNotNone(result.failure)
        assert result.failure is not None
        self.assertEqual(SourceFailureKind.PARTIAL_RESPONSE, result.failure.kind)

    async def test_conditional_304_requires_baseline_parsed_by_same_process(self):
        transport = Transport(
            json_response(200, "N" * 27, **{"Last-Modified": "m1"}),
            raw_response(304),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)
        second = await provider.fetch(fetch_revision=2, last_modified=first.last_modified)

        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertEqual("m1", transport.requests[1].headers["If-Modified-Since"])
        self.assertIsNotNone(second.snapshot)
        assert second.snapshot is not None
        self.assertEqual(ProviderSnapshotKind.NOT_MODIFIED_304, second.snapshot.kind)

    async def test_restart_does_not_trust_persisted_marker_as_cached_baseline(self):
        transport = Transport(raw_response(304))
        result = await adapter(transport).fetch(fetch_revision=10, last_modified="persisted-m1")

        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNotNone(result.failure)
        assert result.failure is not None
        self.assertEqual(SourceFailureKind.INVALID_RESPONSE, result.failure.kind)

    async def test_reused_revision_binds_identity_to_changed_complete_content(self):
        active = await adapter(
            Transport(json_response(200, "A" + "N" * 26, **{"Last-Modified": "m1"}))
        ).fetch(fetch_revision=8, last_modified=None)
        changed_after_restart = await adapter(
            Transport(json_response(200, "N" * 27, **{"Last-Modified": "m2"}))
        ).fetch(fetch_revision=8, last_modified="m1")
        identical_replay = await adapter(
            Transport(json_response(200, "N" * 27, **{"Last-Modified": "m2"}))
        ).fetch(fetch_revision=8, last_modified=None)
        next_empty = await adapter(
            Transport(json_response(200, "N" * 27, **{"Last-Modified": "m3"}))
        ).fetch(fetch_revision=9, last_modified=None)
        confirming_empty = await adapter(
            Transport(json_response(200, "N" * 27, **{"Last-Modified": "m4"}))
        ).fetch(fetch_revision=10, last_modified=None)

        assert active.snapshot is not None
        assert changed_after_restart.snapshot is not None
        assert identical_replay.snapshot is not None
        assert next_empty.snapshot is not None
        assert confirming_empty.snapshot is not None
        self.assertNotEqual(active.snapshot.update_id, changed_after_restart.snapshot.update_id)
        self.assertEqual(
            changed_after_restart.snapshot.update_id,
            identical_replay.snapshot.update_id,
        )

        machine = AlertStateMachine(RegionScope.from_hierarchy("29"))
        started = machine.apply_snapshot(active.snapshot)
        candidate = machine.apply_snapshot(changed_after_restart.snapshot)
        duplicate = machine.apply_snapshot(identical_replay.snapshot)
        next_candidate = machine.apply_snapshot(next_empty.snapshot)
        confirmed = machine.apply_snapshot(confirming_empty.snapshot)
        self.assertEqual(TransitionKind.ALERT_STARTED, started.kind)
        # The changed response is not mistaken for the already committed
        # update. Its reused revision degrades fail-closed, then two later
        # complete observations are still required before all-clear.
        self.assertEqual(TransitionKind.DEGRADED, candidate.kind)
        self.assertEqual(AlertState.STALE, candidate.state)
        self.assertEqual(TransitionKind.NONE, duplicate.kind)
        self.assertEqual(AlertState.STALE, next_candidate.state)
        self.assertEqual(TransitionKind.ALL_CLEAR_CONFIRMED, confirmed.kind)
        self.assertEqual(AlertState.CLEAR_CONFIRMED, machine.state)

    async def test_timeout_rate_limit_and_auth_fail_closed(self):
        timed_out = await adapter(Transport(TimeoutError())).fetch(
            fetch_revision=1, last_modified=None
        )
        limited = await adapter(
            Transport(raw_response(429, **{"Retry-After": "45"}))
        ).fetch(fetch_revision=2, last_modified=None)
        denied = await adapter(Transport(raw_response(403))).fetch(
            fetch_revision=3, last_modified=None
        )

        self.assertEqual(SourceFailureKind.TIMEOUT, timed_out.failure.kind)
        self.assertEqual(45, limited.retry_after_seconds)
        self.assertTrue(denied.open_operator_circuit)


if __name__ == "__main__":
    unittest.main()
