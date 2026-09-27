import json
import unittest
from datetime import UTC, datetime

from time_tl.application.alert_worker import AlertWorker
from time_tl.domain.alerts import RegionScope
from time_tl.infrastructure.alerts import (
    FullTerritoryAlertsInUaAdapter,
    HttpResponse,
    InMemoryAlertRepository,
    InMemorySingletonLease,
)
from time_tl.runtime.region_registry_full import EXPECTED_RECORD_COUNT, load_catalog

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
UIDS = ("1", "3", "4")
OLDER_MARKER = "Thu, 24 Sep 2026 09:00:00 GMT"
MARKER_1 = "Thu, 24 Sep 2026 10:00:00 GMT"
MARKER_2 = "Thu, 24 Sep 2026 11:00:00 GMT"
MARKER_3 = "Thu, 24 Sep 2026 12:00:00 GMT"


class Transport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def response(status: int, statuses: str | None = None, marker: str | None = MARKER_1):
    headers = {} if marker is None else {"Last-Modified": marker}
    body = b"" if statuses is None else json.dumps(statuses).encode()
    return HttpResponse(status, headers, body)


def adapter(transport: Transport, uids: tuple[str, ...] = UIDS):
    return FullTerritoryAlertsInUaAdapter(
        token="secret-not-logged",
        transport=transport,
        region_uids=uids,
        now=lambda: NOW,
    )


class FullTerritoryProviderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _hierarchy_statuses(values: dict[str, str]) -> str:
        statuses = [" "] * 5
        for uid in UIDS:
            statuses[int(uid)] = "N"
        for uid, status in values.items():
            statuses[int(uid)] = status
        return "".join(statuses)

    async def _hierarchy_outbox(self, values: dict[str, str]):
        provider = adapter(Transport(response(200, self._hierarchy_statuses(values))))
        repository = InMemoryAlertRepository(
            [("student", RegionScope.from_hierarchy("4", ancestors=("3", "1")))]
        )
        worker = AlertWorker(
            provider=provider,
            repository=repository,
            lease=InMemorySingletonLease(),
            monotonic=lambda: 100.0,
        )
        await worker.run_once()
        return await repository.outbox()

    async def test_numeric_uid_index_mapping_covers_every_pinned_catalogue_uid(self):
        catalog = load_catalog()
        uids = tuple(record.uid for record in catalog.records)
        self.assertEqual(EXPECTED_RECORD_COUNT, len(uids))
        self.assertEqual(1911, max(map(int, uids)))

        statuses = [" "] * 1912
        for uid in uids:
            statuses[int(uid)] = "N"
        statuses[31] = "A"
        statuses[1903] = "P"
        transport = Transport(response(200, "".join(statuses)))

        result = await adapter(transport, uids).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual(frozenset(uids), result.snapshot.covered_region_ids)
        self.assertEqual(
            [
                ("31", "air_raid", "air-raid:31"),
                ("1903", "air_raid_partial", "air-raid:1903"),
            ],
            [
                (episode.location_uid, episode.alert_type, episode.episode_id)
                for episode in result.snapshot.active_episodes
            ],
        )
        self.assertEqual(
            "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts.json",
            transport.requests[0].url,
        )
        self.assertNotIn("secret-not-logged", transport.requests[0].url)

    async def test_non_space_unknown_uid_fails_closed(self):
        statuses = [" "] * 5
        for uid in UIDS:
            statuses[int(uid)] = "N"
        statuses[2] = "A"

        result = await adapter(Transport(response(200, "".join(statuses)))).fetch(
            fetch_revision=1, last_modified=None
        )

        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.failure)

    async def test_payload_longer_than_pinned_uid_range_fails_closed(self):
        statuses = [" "] * 6
        for uid in UIDS:
            statuses[int(uid)] = "N"

        result = await adapter(Transport(response(200, "".join(statuses)))).fetch(
            fetch_revision=1, last_modified=None
        )

        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.failure)

    async def test_catalogue_drift_cannot_turn_active_scope_into_all_clear(self):
        active = self._hierarchy_statuses({"4": "A"})
        drift = list(self._hierarchy_statuses({}))
        drift[2] = "A"
        transport = Transport(
            response(200, active, marker=MARKER_1),
            response(200, "".join(drift), marker=MARKER_2),
            response(200, "".join(drift), marker=MARKER_3),
        )
        repository = InMemoryAlertRepository(
            [("student", RegionScope.from_hierarchy("4", ancestors=("3", "1")))]
        )
        worker = AlertWorker(
            provider=adapter(transport),
            repository=repository,
            lease=InMemorySingletonLease(),
            monotonic=lambda: 100.0,
        )

        await worker.run_once()
        await worker.run_once()
        await worker.run_once()

        outbox = await repository.outbox()
        self.assertEqual(1, len(outbox))
        self.assertEqual("ALERT_STARTED", outbox[0].transition_kind.value)
        snapshot = await repository.snapshot_for("student")
        self.assertNotEqual("CLEAR_CONFIRMED", snapshot.state.value)

    async def test_snapshot_shorter_than_maximum_known_uid_fails_closed(self):
        result = await adapter(Transport(response(200, " N N"))).fetch(
            fetch_revision=1, last_modified=None
        )

        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.failure)

    async def test_invalid_character_at_unknown_uid_fails_closed(self):
        result = await adapter(Transport(response(200, " N NN?"))).fetch(
            fetch_revision=1, last_modified=None
        )

        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.failure)

    async def test_space_at_known_uid_fails_closed(self):
        result = await adapter(Transport(response(200, " N N "))).fetch(
            fetch_revision=1, last_modified=None
        )

        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.failure)

    async def test_missing_last_modified_and_non_string_fail_closed(self):
        no_marker = await adapter(Transport(response(200, " A PN", marker=None))).fetch(
            fetch_revision=1, last_modified=None
        )
        non_string = await adapter(
            Transport(HttpResponse(200, {"Last-Modified": MARKER_1}, b"[]"))
        ).fetch(fetch_revision=1, last_modified=None)
        self.assertIsNotNone(no_marker.failure)
        self.assertIsNotNone(non_string.failure)

    async def test_304_requires_same_process_complete_200(self):
        restarted_transport = Transport(response(304, marker=None))
        restarted = await adapter(restarted_transport).fetch(
            fetch_revision=2, last_modified="persisted"
        )
        self.assertNotIn("If-Modified-Since", restarted_transport.requests[0].headers)
        self.assertIsNotNone(restarted.failure)

        transport = Transport(
            response(200, " A PN", marker=MARKER_1),
            response(304, marker=None),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)
        second = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        self.assertIsNotNone(first.snapshot)
        self.assertIsNotNone(second.snapshot)
        self.assertEqual(MARKER_1, transport.requests[1].headers["If-Modified-Since"])
        self.assertEqual(MARKER_1, second.last_modified)

    async def test_restart_uses_persisted_marker_as_floor_for_older_clear_replay(self):
        transport = Transport(response(200, " N NN", marker=OLDER_MARKER))
        provider = adapter(transport)

        replay = await provider.fetch(fetch_revision=8, last_modified=MARKER_2)

        self.assertEqual(1, len(transport.requests))
        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNone(replay.snapshot)
        self.assertIsNotNone(replay.failure)

    async def test_active_newer_then_older_clear_replay_fails_closed(self):
        transport = Transport(
            response(200, " A NN", marker=MARKER_2),
            response(200, " N NN", marker=MARKER_1),
        )
        provider = adapter(transport)
        active = await provider.fetch(fetch_revision=1, last_modified=None)

        replay = await provider.fetch(fetch_revision=2, last_modified=MARKER_2)

        self.assertIsNotNone(active.snapshot)
        self.assertIsNone(replay.snapshot)
        self.assertIsNotNone(replay.failure)

    async def test_malformed_200_marker_fails_closed(self):
        malformed_response = await adapter(
            Transport(response(200, " A PN", marker="not-an-http-date"))
        ).fetch(fetch_revision=1, last_modified=None)
        malformed_floor = await adapter(Transport(response(200, " A PN", marker=MARKER_1))).fetch(
            fetch_revision=1, last_modified="not-an-http-date"
        )

        self.assertIsNone(malformed_response.snapshot)
        self.assertIsNotNone(malformed_response.failure)
        self.assertIsNone(malformed_floor.snapshot)
        self.assertIsNotNone(malformed_floor.failure)

    async def test_304_rejects_marker_inconsistent_with_conditional(self):
        transport = Transport(
            response(200, " A PN", marker=MARKER_1),
            response(304, marker=MARKER_2),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)

        inconsistent = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)

        self.assertIsNotNone(first.snapshot)
        self.assertIsNone(inconsistent.snapshot)
        self.assertIsNotNone(inconsistent.failure)

    async def test_equal_marker_with_different_content_fails_closed(self):
        transport = Transport(
            response(200, " A NN", marker=MARKER_1),
            response(200, " N NN", marker=MARKER_1),
        )
        provider = adapter(transport)
        first = await provider.fetch(fetch_revision=1, last_modified=None)

        changed = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)

        self.assertIsNotNone(first.snapshot)
        self.assertIsNone(changed.snapshot)
        self.assertIsNotNone(changed.failure)

    async def test_partial_to_whole_keeps_episode_identity_and_dedupes_transition(self):
        transport = Transport(
            response(200, " P NN", marker=MARKER_1),
            response(200, " A NN", marker=MARKER_2),
        )
        provider = adapter(transport)
        repository = InMemoryAlertRepository([("student", RegionScope.from_hierarchy("1"))])
        worker = AlertWorker(
            provider=provider,
            repository=repository,
            lease=InMemorySingletonLease(),
            monotonic=lambda: 100.0,
        )

        await worker.run_once()
        await worker.run_once()

        outbox = await repository.outbox()
        self.assertEqual(1, len(outbox))
        snapshot = await repository.snapshot_for("student")
        self.assertEqual(
            {"air-raid:1"},
            {item.episode.episode_id for item in snapshot.active_episodes},
        )

    async def test_official_a_p_contract_across_oblast_raion_and_hromada(self):
        for active_uid in ("1", "3", "4"):
            with self.subTest(active_uid=active_uid):
                outbox = await self._hierarchy_outbox({active_uid: "A"})
                self.assertEqual(1, len(outbox))

        for partial_parent_uid in ("1", "3"):
            with self.subTest(partial_parent_uid=partial_parent_uid):
                outbox = await self._hierarchy_outbox({partial_parent_uid: "P"})
                self.assertEqual((), outbox)

        selected_partial = await self._hierarchy_outbox({"4": "P"})
        self.assertEqual(1, len(selected_partial))

        simultaneous = await self._hierarchy_outbox({"1": "A", "3": "A", "4": "A"})
        self.assertEqual(1, len(simultaneous))

    def test_registry_rejects_duplicate_numeric_uid(self):
        with self.assertRaises(ValueError):
            adapter(Transport(), ("1", "01"))


if __name__ == "__main__":
    unittest.main()
