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
MARKER_3 = "Sun, 20 Sep 2026 12:00:00 GMT"
SIGNED_INT64_MAX = (1 << 63) - 1
SYNTHETIC_UNKNOWN_UID = "999999"
REGION_TYPES = {
    "1903": "Громада",
    "1803": "Район",
    "16": "Область",
    "31": "Місто з спеціальним статусом",
}
META_TIMESTAMP = "2026/09/20 15:00:00 +0300"


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


def raw_response(body, marker=MARKER_1):
    return HttpResponse(200, {"Last-Modified": marker}, body)


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


def mixed_with_synthetic_unknown(
    *,
    known_uid: str = "1903",
    known_location_type: str = "hromada",
    unknown_alert_type: str = "air_raid",
):
    payload = active(uid=known_uid, location_type=known_location_type)
    unknown = dict(payload["alerts"][0])
    unknown.update(
        id=11,
        location_uid=SYNTHETIC_UNKNOWN_UID,
        location_title="Синтетична невідома тестова локація",
        alert_type=unknown_alert_type,
    )
    payload["alerts"].append(unknown)
    return payload


def official_envelope(payload=None, *, meta=None, disclaimer="Official alerts.in.ua data"):
    payload = active() if payload is None else payload
    return {
        "alerts": payload["alerts"],
        "meta": {"last_updated_at": META_TIMESTAMP} if meta is None else meta,
        "disclaimer": disclaimer,
    }


def duplicate_root_alerts_payload():
    encoded_alerts = json.dumps(active()["alerts"]).encode()
    return b'{"alerts":' + encoded_alerts + b',"alerts":[]}'


def deeply_nested_metadata_payload(depth=10_000):
    timestamp = json.dumps(META_TIMESTAMP).encode()
    disclaimer = json.dumps("Official alerts.in.ua data").encode()
    return (
        b'{"alerts":[],"meta":{"last_updated_at":'
        + timestamp
        + b',"nested":'
        + (b"[" * depth)
        + b"null"
        + (b"]" * depth)
        + b'},"disclaimer":'
        + disclaimer
        + b"}"
    )


def adapter(transport):
    return AlertsInUaAdapter(
        token="secret-not-logged",
        transport=transport,
        region_types=REGION_TYPES,
        now=lambda: NOW,
    )


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_sdk_envelope_is_a_complete_snapshot(self):
        result = await adapter(
            Transport(
                response(
                    200,
                    official_envelope(),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual("10", result.snapshot.active_episodes[0].episode_id)

    async def test_bounded_additional_metadata_is_digest_only(self):
        metadata = {
            "last_updated_at": META_TIMESTAMP,
            "schema_version": 3,
            "provider_ok": True,
            "optional_value": None,
            "ratio": 0.5,
            "nested": {"level_2": [{"level_3": "complete"}]},
        }
        provider = adapter(Transport())
        episodes, digest = provider._parse_active_payload(
            json.dumps(official_envelope(meta=metadata)).encode()
        )
        reordered = dict(reversed(tuple(metadata.items())))
        reordered_episodes, reordered_digest = provider._parse_active_payload(
            json.dumps(official_envelope(meta=reordered)).encode()
        )
        changed = dict(metadata)
        changed["nested"] = {"level_2": [{"level_3": "changed"}]}
        changed_episodes, changed_digest = provider._parse_active_payload(
            json.dumps(official_envelope(meta=changed)).encode()
        )

        self.assertEqual(episodes, reordered_episodes)
        self.assertEqual(episodes, changed_episodes)
        self.assertEqual(digest, reordered_digest)
        self.assertNotEqual(digest, changed_digest)
        self.assertEqual(("10",), tuple(item.episode_id for item in episodes))

    async def test_metadata_accepts_documented_boundaries(self):
        exact_depth_three = {"one": {"two": {"three": "ok"}}}
        metadata = {
            "last_updated_at": META_TIMESTAMP,
            "field_one": exact_depth_three,
            "field_two": list(range(32)),
            "field_three": -(1 << 63),
            "field_four": (1 << 63) - 1,
            "field_five": "x" * AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS,
            "field_six": False,
            "field_seven": 1.25,
            "k" * 64: None,
        }
        result = await adapter(
            Transport(
                response(
                    200,
                    official_envelope(meta=metadata),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual("10", result.snapshot.active_episodes[0].episode_id)

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

    def test_parser_accepts_only_positive_signed_int64_alert_ids(self):
        provider = adapter(Transport())
        for alert_id in (1, SIGNED_INT64_MAX):
            with self.subTest(valid=alert_id):
                episodes, _digest = provider._parse_active_payload(
                    json.dumps(active(alert_id)).encode()
                )
                self.assertEqual(str(alert_id), episodes[0].episode_id)

        for alert_id in (False, 0, -1, SIGNED_INT64_MAX + 1):
            with self.subTest(invalid=alert_id), self.assertRaises(ValueError):
                provider._parse_active_payload(json.dumps(active(alert_id)).encode())

    async def test_bounded_undocumented_alert_fields_are_discarded_and_not_digested(self):
        payload = active()
        payload["alerts"][0]["location_oblast_uid"] = "first-ignored-value"
        payload["alerts"][0]["provider_private_field"] = "must-not-leak"
        second_row = dict(payload["alerts"][0])
        second_row.update(id=11, alert_type="chemical")
        second_row["location_oblast_uid"] = {"second": "ignored-value"}
        second_row["provider_private_field"] = {"same_key": "different-private-value"}
        payload["alerts"].append(second_row)
        changed = active()
        changed["alerts"][0]["location_oblast_uid"] = False
        changed["alerts"][0]["provider_private_field"] = {"nested": [False, None]}
        changed_second_row = dict(changed["alerts"][0])
        changed_second_row.update(id=11, alert_type="chemical")
        changed_second_row["location_oblast_uid"] = ["changed", 16]
        changed_second_row["provider_private_field"] = "changed-secret"
        changed["alerts"].append(changed_second_row)
        provider = adapter(Transport())

        episodes, digest = provider._parse_active_payload(json.dumps(payload).encode())
        changed_episodes, changed_digest = provider._parse_active_payload(
            json.dumps(changed).encode()
        )

        self.assertEqual(episodes, changed_episodes)
        self.assertEqual(digest, changed_digest)
        self.assertEqual(("10",), tuple(item.episode_id for item in episodes))

    async def test_undocumented_alert_fields_are_structurally_bounded(self):
        too_many = active()
        too_many["alerts"][0].update(
            {
                f"private_{index}": index
                for index in range(AlertsInUaAdapter.MAX_ALERT_EXTRA_FIELDS + 1)
            }
        )
        oversized_key = active()
        oversized_key["alerts"][0]["x" * 65] = "must-not-leak"
        too_deep = active()
        too_deep["alerts"][0]["provider_data"] = {
            "two": {"three": {"four": {"five": "must-not-leak"}}}
        }
        inconsistent = active()
        inconsistent["alerts"][0]["provider_data"] = None
        inconsistent_second_row = dict(inconsistent["alerts"][0])
        inconsistent_second_row["id"] = 11
        del inconsistent_second_row["provider_data"]
        inconsistent_second_row["different_provider_data"] = None
        inconsistent["alerts"].append(inconsistent_second_row)
        samples = (
            (too_many, "alert_extra_fields_limit"),
            (oversized_key, "alert_extra_key_invalid"),
            (too_deep, "alert_extra_value_invalid"),
            (inconsistent, "alert_extra_key_inconsistent"),
        )

        for payload, code in samples:
            with self.subTest(code=code):
                result = await adapter(
                    Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNone(result.snapshot)
                self.assertEqual(code, result.diagnostic_code)
                summary = result.sanitized_failure()
                assert summary is not None
                self.assertNotIn("provider_data", summary)
                self.assertNotIn("must-not-leak", summary)

    async def test_ignored_field_cannot_mask_malformed_documented_field(self):
        payload = active(alert_level="green")
        payload["alerts"][0]["undocumented_provider_data"] = "must-not-leak"

        result = await adapter(
            Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNone(result.snapshot)
        self.assertEqual("alert_level_enum_invalid", result.diagnostic_code)
        summary = result.sanitized_failure()
        assert summary is not None
        self.assertNotIn("undocumented_provider_data", summary)
        self.assertNotIn("must-not-leak", summary)

    async def test_unknown_uid_diagnostic_never_exposes_uid(self):
        private_uid = "999999"
        result = await adapter(
            Transport(
                response(
                    200,
                    active(uid=private_uid),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertEqual("location_uid_unknown", result.diagnostic_code)
        self.assertEqual(1, result.diagnostic_count)
        summary = result.sanitized_failure()
        assert summary is not None
        self.assertNotIn(private_uid, summary)

    def test_known_ignored_oblast_uid_forms_do_not_change_episodes_or_digest(self):
        absent = active()
        del absent["alerts"][0]["location_oblast_uid"]
        provider = adapter(Transport())
        expected_episodes, expected_digest = provider._parse_active_payload(
            json.dumps(absent).encode()
        )
        values = (
            None,
            False,
            -16,
            16,
            16.5,
            "not-authoritative",
            ["bounded", 16, None],
            {"nested": {"value": "bounded"}},
        )

        for value in values:
            with self.subTest(value=value):
                payload = active()
                payload["alerts"][0]["location_oblast_uid"] = value
                episodes, digest = provider._parse_active_payload(json.dumps(payload).encode())
                self.assertEqual(expected_episodes, episodes)
                self.assertEqual(expected_digest, digest)

    def test_known_ignored_oblast_uid_coexists_with_one_generic_extra(self):
        payload = active()
        payload["alerts"][0]["location_oblast_uid"] = {"private": [16, False]}
        payload["alerts"][0]["provider_private_field"] = "must-not-leak"
        absent = active()
        del absent["alerts"][0]["location_oblast_uid"]
        provider = adapter(Transport())

        episodes, digest = provider._parse_active_payload(json.dumps(payload).encode())
        absent_episodes, absent_digest = provider._parse_active_payload(
            json.dumps(absent).encode()
        )

        self.assertEqual(absent_episodes, episodes)
        self.assertEqual(absent_digest, digest)

    async def test_known_ignored_oblast_uid_bounds_fail_closed_and_redacted(self):
        too_many_nodes = [[None, None, None, None] for _ in range(32)]
        too_large = ["x" * AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS for _ in range(20)]
        samples = (
            (1 << 63, "alert_ignored_value_invalid"),
            (float("nan"), "alert_ignored_value_invalid"),
            (
                "x" * (AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS + 1),
                "alert_ignored_value_invalid",
            ),
            (
                [None] * (AlertsInUaAdapter.MAX_META_EXTRA_CONTAINER_ELEMENTS + 1),
                "alert_ignored_value_invalid",
            ),
            ({"two": {"three": {"four": {"five": None}}}}, "alert_ignored_value_invalid"),
            (too_many_nodes, "alert_ignored_value_invalid"),
            (too_large, "alert_ignored_serialized_size"),
        )

        for value, code in samples:
            with self.subTest(code=code):
                payload = active()
                payload["alerts"][0]["location_oblast_uid"] = value
                result = await adapter(
                    Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)

                self.assertIsNone(result.snapshot)
                self.assertEqual(code, result.diagnostic_code)
                self.assertEqual(1, result.diagnostic_count)
                summary = result.sanitized_failure()
                assert summary is not None
                self.assertNotIn("location_oblast_uid", summary)
                self.assertNotIn("must-not-leak", summary)

    async def test_non_air_row_still_validates_known_ignored_value_bounds(self):
        payload = active(alert_type="chemical")
        payload["alerts"][0]["location_oblast_uid"] = [None] * 33

        result = await adapter(
            Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNone(result.snapshot)
        self.assertEqual("alert_ignored_value_invalid", result.diagnostic_code)

    async def test_ignored_oblast_uid_preserves_same_marker_replay(self):
        absent = active()
        del absent["alerts"][0]["location_oblast_uid"]
        ignored = active()
        ignored["alerts"][0]["location_oblast_uid"] = {"not": "authoritative"}
        provider = adapter(
            Transport(
                response(200, absent, **{"Last-Modified": MARKER_1}),
                response(200, ignored, **{"Last-Modified": MARKER_1}),
            )
        )

        baseline = await provider.fetch(fetch_revision=1, last_modified=None)
        replay = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)

        self.assertIsNotNone(baseline.snapshot)
        self.assertIsNotNone(replay.snapshot)
        self.assertIsNone(replay.failure)

    async def test_invalid_ignored_oblast_uid_does_not_advance_validated_baseline(self):
        invalid = active()
        invalid["alerts"][0]["location_oblast_uid"] = "x" * 2049
        transport = Transport(
            response(200, active(), **{"Last-Modified": MARKER_1}),
            response(200, invalid, **{"Last-Modified": MARKER_2}),
            response(304, **{"Last-Modified": MARKER_1}),
        )
        provider = adapter(transport)

        baseline = await provider.fetch(fetch_revision=1, last_modified=None)
        rejected = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        unchanged = await provider.fetch(fetch_revision=3, last_modified=MARKER_1)

        self.assertIsNotNone(baseline.snapshot)
        self.assertIsNone(rejected.snapshot)
        self.assertEqual("alert_ignored_value_invalid", rejected.diagnostic_code)
        self.assertIsNotNone(unchanged.snapshot)
        assert unchanged.snapshot is not None
        self.assertEqual("NOT_MODIFIED_304", unchanged.snapshot.kind.value)
        self.assertEqual(MARKER_1, unchanged.last_modified)

    async def test_diagnostic_taxonomy_identifies_rejected_predicate(self):
        missing = active()
        del missing["alerts"][0]["updated_at"]
        mismatch = active(uid="1803", location_type="hromada")
        naive = active()
        naive["alerts"][0]["started_at"] = "2026-09-20T10:00:00"
        threat = active()
        threat["alerts"][0]["threats"] = [
            {
                "threat_type": "not-documented",
                "level": "yellow",
                "started_at": "2026-09-20T10:00:01Z",
            }
        ]
        samples = (
            ([], "root_not_object", 1),
            ({}, "root_fields_missing", 1),
            (
                {"alerts": [], "unexpected": True, "another": False},
                "root_fields_unknown",
                2,
            ),
            (missing, "alert_fields_missing", 1),
            (active(False), "alert_id_invalid", 1),
            (active(uid="01903"), "location_uid_format_invalid", 1),
            (mismatch, "location_type_catalogue_mismatch", 1),
            (active(alert_type="new_alert_kind"), "alert_type_enum_invalid", 1),
            (active(alert_level="green"), "alert_level_enum_invalid", 1),
            (naive, "started_at_timezone_missing", 1),
            (threat, "threat_type_enum_invalid", 1),
        )

        for payload, code, count in samples:
            with self.subTest(code=code):
                result = await adapter(
                    Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertEqual(code, result.diagnostic_code)
                self.assertEqual(count, result.diagnostic_count)

    async def test_official_envelope_metadata_is_strict_and_redacted(self):
        samples = (
            (
                {"alerts": [], "meta": {"last_updated_at": META_TIMESTAMP}},
                "root_fields_missing",
                1,
            ),
            (official_envelope(meta=[]), "meta_type_invalid", 1),
            (official_envelope(meta={}), "meta_fields_missing", 1),
            (
                official_envelope(meta={"last_updated_at": 123}),
                "meta_last_updated_at_type_invalid",
                1,
            ),
            (
                official_envelope(meta={"last_updated_at": "2026-09-20T12:00:00Z"}),
                "meta_last_updated_at_format_invalid",
                1,
            ),
            (
                official_envelope(meta={"last_updated_at": "2026/02/30 12:00:00 +0000"}),
                "meta_last_updated_at_format_invalid",
                1,
            ),
            (official_envelope(disclaimer=None), "disclaimer_type_invalid", 1),
            (official_envelope(disclaimer=""), "disclaimer_length_invalid", 1),
            (
                official_envelope(
                    disclaimer="x" * (AlertsInUaAdapter.MAX_DISCLAIMER_CHARS + 1)
                ),
                "disclaimer_length_invalid",
                1,
            ),
            (
                {**official_envelope(), "unknown": "must-not-leak"},
                "root_fields_unknown",
                1,
            ),
        )

        for payload, code, count in samples:
            with self.subTest(code=code):
                result = await adapter(
                    Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertEqual(code, result.diagnostic_code)
                self.assertEqual(count, result.diagnostic_count)
                summary = result.sanitized_failure()
                assert summary is not None
                self.assertNotIn("must-not-leak", summary)
                self.assertNotIn(META_TIMESTAMP, summary)

    async def test_additional_metadata_violations_are_redacted_and_fail_closed(self):
        base = {"last_updated_at": META_TIMESTAMP}
        too_many = {**base, **{f"field_{index}": index for index in range(9)}}
        too_many_nodes = {
            **base,
            **{f"field_{index}": list(range(16)) for index in range(8)},
        }
        too_large = {
            **base,
            **{
                f"field_{index}": {
                    "first": "x" * AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS,
                    "second": "y" * AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS,
                }
                for index in range(8)
            },
        }
        samples = (
            (too_many, "meta_extra_fields_limit", 9),
            (
                {**base, "PrivateProviderField": "must-not-leak"},
                "meta_extra_key_invalid",
                1,
            ),
            ({**base, "k" * 65: "must-not-leak"}, "meta_extra_key_invalid", 1),
            ({**base, "private__field": "must-not-leak"}, "meta_extra_key_invalid", 1),
            ({**base, "lastupdated_at": "must-not-leak"}, "meta_extra_key_conflict", 1),
            (
                {
                    **base,
                    "nested": {"two": {"three": {"four": {"five": "too-deep"}}}},
                },
                "meta_extra_depth_limit",
                1,
            ),
            (
                {
                    **base,
                    "items": list(
                        range(AlertsInUaAdapter.MAX_META_EXTRA_CONTAINER_ELEMENTS + 1)
                    ),
                },
                "meta_extra_container_limit",
                1,
            ),
            (too_many_nodes, "meta_extra_nodes_limit", 1),
            (
                {
                    **base,
                    "text": "x" * (AlertsInUaAdapter.MAX_META_EXTRA_STRING_CHARS + 1),
                },
                "meta_extra_string_length",
                1,
            ),
            ({**base, "number": -(1 << 63) - 1}, "meta_extra_integer_range", 1),
            ({**base, "number": (1 << 63)}, "meta_extra_integer_range", 1),
            ({**base, "number": float("nan")}, "meta_extra_float_non_finite", 1),
            ({**base, "number": float("inf")}, "meta_extra_float_non_finite", 1),
            (too_large, "meta_extra_serialized_size", 1),
        )

        for metadata, code, count in samples:
            with self.subTest(code=code):
                result = await adapter(
                    Transport(
                        response(
                            200,
                            official_envelope(meta=metadata),
                            **{"Last-Modified": MARKER_1},
                        )
                    )
                ).fetch(fetch_revision=1, last_modified=None)
                self.assertIsNotNone(result.failure)
                self.assertEqual(code, result.diagnostic_code)
                self.assertEqual(count, result.diagnostic_count)
                summary = result.sanitized_failure()
                assert summary is not None
                self.assertNotIn("PrivateProviderField", summary)
                self.assertNotIn("lastupdated_at", summary)
                self.assertNotIn("must-not-leak", summary)

    async def test_duplicate_json_object_keys_at_every_level_fail_closed(self):
        encoded_alerts = json.dumps(active()["alerts"]).encode()
        encoded_alert = json.dumps(active()["alerts"][0]).encode()
        duplicate_id_alert = encoded_alert.replace(
            b'"id": 10',
            b'"id": 10, "id": 11',
            1,
        )
        timestamp = json.dumps(META_TIMESTAMP).encode()
        disclaimer = json.dumps("Official alerts.in.ua data").encode()
        payloads = (
            duplicate_root_alerts_payload(),
            b'{"alerts":[' + duplicate_id_alert + b"]}",
            (
                b'{"alerts":'
                + encoded_alerts
                + b',"meta":{"last_updated_at":'
                + timestamp
                + b',"last_updated_at":"must-not-leak"},"disclaimer":'
                + disclaimer
                + b"}"
            ),
            (
                b'{"alerts":'
                + encoded_alerts
                + b',"meta":{"last_updated_at":'
                + timestamp
                + b',"provider_ok":true,"provider_ok":false},"disclaimer":'
                + disclaimer
                + b"}"
            ),
            (
                b'{"alerts":'
                + encoded_alerts
                + b',"meta":{"last_updated_at":'
                + timestamp
                + b',"nested":{"private_flag":true,"private_flag":false}},'
                + b'"disclaimer":'
                + disclaimer
                + b"}"
            ),
        )

        for payload in payloads:
            with self.subTest(payload_length=len(payload)):
                result = await adapter(Transport(raw_response(payload))).fetch(
                    fetch_revision=1,
                    last_modified=None,
                )
                self.assertIsNotNone(result.failure)
                self.assertEqual("json_object_keys_duplicate", result.diagnostic_code)
                self.assertEqual(1, result.diagnostic_count)
                summary = result.sanitized_failure()
                self.assertEqual(
                    "kind=INVALID_RESPONSE code=json_object_keys_duplicate count=1",
                    summary,
                )
                assert summary is not None
                self.assertNotIn("alerts", summary)
                self.assertNotIn("last_updated_at", summary)
                self.assertNotIn("provider_ok", summary)
                self.assertNotIn("private_flag", summary)
                self.assertNotIn("must-not-leak", summary)

    async def test_excessively_deep_raw_json_is_redacted_invalid_response(self):
        result = await adapter(
            Transport(raw_response(deeply_nested_metadata_payload()))
        ).fetch(fetch_revision=1, last_modified=None)

        self.assertIsNotNone(result.failure)
        self.assertEqual("json_depth_unprocessable", result.diagnostic_code)
        self.assertEqual(1, result.diagnostic_count)
        self.assertEqual(
            "kind=INVALID_RESPONSE code=json_depth_unprocessable count=1",
            result.sanitized_failure(),
        )

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

    async def test_ignored_alert_field_does_not_change_validated_content_digest(self):
        candidate = active()
        candidate["alerts"][0]["calculated"] = False
        transport = Transport(
            response(200, active(), **{"Last-Modified": MARKER_1}),
            response(200, candidate, **{"Last-Modified": MARKER_1}),
            response(304),
        )
        provider = adapter(transport)

        baseline = await provider.fetch(fetch_revision=1, last_modified=None)
        ignored = await provider.fetch(fetch_revision=2, last_modified=MARKER_1)
        unchanged = await provider.fetch(fetch_revision=3, last_modified=MARKER_1)

        self.assertIsNotNone(baseline.snapshot)
        self.assertIsNotNone(ignored.snapshot)
        self.assertIsNone(ignored.failure)
        self.assertIsNotNone(unchanged.snapshot)
        assert unchanged.snapshot is not None
        self.assertEqual("NOT_MODIFIED_304", unchanged.snapshot.kind.value)
        self.assertEqual(MARKER_1, unchanged.last_modified)
        self.assertEqual(MARKER_1, transport.requests[2].headers["If-Modified-Since"])

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
        return self.worker_for(self.repo, transport)

    def worker_for(self, repository, transport):
        return AlertWorker(
            provider=adapter(transport),
            repository=repository,
            lease=InMemorySingletonLease(),
            monotonic=lambda: self.clock,
            jitter=lambda a, b: 7.0,
        )

    async def test_mixed_unknown_uid_globally_degrades_and_discards_known_rows(self):
        second_scope = RegionScope.from_hierarchy("31")
        repository = InMemoryAlertRepository(
            [("community", self.scope), ("special-city", second_scope)]
        )

        await self.worker_for(
            repository,
            Transport(
                response(
                    200,
                    mixed_with_synthetic_unknown(),
                    **{"Last-Modified": MARKER_1},
                )
            ),
        ).run_once()

        for scope_id in ("community", "special-city"):
            snapshot = await repository.snapshot_for(scope_id)
            assert snapshot is not None
            self.assertEqual(AlertState.UNKNOWN, snapshot.state)
            self.assertEqual((), snapshot.active_episodes)
        self.assertEqual((), await repository.outbox())

    async def test_unknown_uid_after_known_active_degrades_every_scope_and_preserves_active(self):
        second_scope = RegionScope.from_hierarchy("31")
        repository = InMemoryAlertRepository(
            [("community", self.scope), ("special-city", second_scope)]
        )
        worker = self.worker_for(
            repository,
            Transport(
                response(200, active(), **{"Last-Modified": MARKER_1}),
                response(
                    200,
                    mixed_with_synthetic_unknown(),
                    **{"Last-Modified": MARKER_2},
                ),
            ),
        )

        await worker.run_once()
        initial_outbox = await repository.outbox()
        self.clock += 15
        await worker.run_once()

        community = await repository.snapshot_for("community")
        special_city = await repository.snapshot_for("special-city")
        assert community is not None and special_city is not None
        self.assertEqual(AlertState.STALE, community.state)
        self.assertEqual(1, len(community.active_episodes))
        self.assertEqual(AlertState.STALE, special_city.state)
        self.assertEqual((), special_city.active_episodes)
        self.assertEqual(MARKER_1, (await repository.poll_state()).last_modified)
        self.assertEqual(initial_outbox, await repository.outbox())
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await repository.outbox()
            )
        )

    async def test_repeated_unknown_uid_polls_continue_fail_closed_without_outbox(self):
        worker = self.worker(
            Transport(
                response(
                    200,
                    mixed_with_synthetic_unknown(),
                    **{"Last-Modified": MARKER_1},
                ),
                response(
                    200,
                    mixed_with_synthetic_unknown(unknown_alert_type="chemical"),
                    **{"Last-Modified": MARKER_2},
                ),
            )
        )

        first_delay = await worker.run_once()
        self.clock += first_delay
        second_delay = await worker.run_once()

        snapshot = await self.repo.snapshot_for("student-1")
        poll_state = await self.repo.poll_state()
        assert snapshot is not None
        self.assertEqual(AlertState.UNKNOWN, snapshot.state)
        self.assertEqual(2, poll_state.fetch_revision)
        self.assertEqual(2, poll_state.consecutive_failures)
        self.assertGreaterEqual(second_delay, first_delay)
        self.assertEqual((), await self.repo.outbox())

    async def test_unknown_uid_then_empty_requires_normal_two_observation_clear(self):
        no_air_alert = active(alert_type="chemical")
        worker = self.worker(
            Transport(
                response(200, active(), **{"Last-Modified": MARKER_1}),
                response(
                    200,
                    mixed_with_synthetic_unknown(),
                    **{"Last-Modified": MARKER_2},
                ),
                response(200, no_air_alert, **{"Last-Modified": MARKER_3}),
                response(304, **{"Last-Modified": MARKER_3}),
            )
        )

        await worker.run_once()
        self.clock += 15
        await worker.run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)
        self.clock += 15
        await worker.run_once()
        first_empty = await self.repo.snapshot_for("student-1")
        assert first_empty is not None
        self.assertEqual(AlertState.STALE, first_empty.state)
        self.assertTrue(first_empty.pending_clear_candidate)
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )
        self.clock += 15
        await worker.run_once()
        self.assertEqual(
            AlertState.CLEAR_CONFIRMED,
            (await self.repo.snapshot_for("student-1")).state,
        )
        self.assertEqual(
            1,
            sum(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            ),
        )

    async def test_restart_then_unknown_uid_preserves_stale_active_without_outbox(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        initial_outbox = await self.repo.outbox()
        await self.repo.apply_process_restart_atomically()
        self.clock += 15

        await self.worker(
            Transport(
                response(
                    200,
                    mixed_with_synthetic_unknown(),
                    **{"Last-Modified": MARKER_2},
                )
            )
        ).run_once()

        snapshot = await self.repo.snapshot_for("student-1")
        assert snapshot is not None
        self.assertEqual(AlertState.STALE, snapshot.state)
        self.assertEqual(1, len(snapshot.active_episodes))
        self.assertEqual(initial_outbox, await self.repo.outbox())

    async def test_unknown_uid_blocks_known_parent_and_child_active_rows(self):
        scope = RegionScope.from_hierarchy(
            "1803",
            ancestors=("16",),
            descendants=("1903",),
        )
        for uid, location_type in (("16", "oblast"), ("1903", "hromada")):
            with self.subTest(uid=uid):
                repository = InMemoryAlertRepository([("student", scope)])
                await self.worker_for(
                    repository,
                    Transport(
                        response(
                            200,
                            mixed_with_synthetic_unknown(
                                known_uid=uid,
                                known_location_type=location_type,
                            ),
                            **{"Last-Modified": MARKER_1},
                        )
                    ),
                ).run_once()

                snapshot = await repository.snapshot_for("student")
                assert snapshot is not None
                self.assertEqual(AlertState.UNKNOWN, snapshot.state)
                self.assertEqual((), await repository.outbox())

    async def test_restart_rejects_unverifiable_304_without_false_clear(self):
        integer_oblast_active = active()
        integer_oblast_active["alerts"][0]["location_oblast_uid"] = 16
        await self.worker(
            Transport(response(200, integer_oblast_active, **{"Last-Modified": MARKER_1}))
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

    async def test_unbounded_ignored_oblast_uid_after_active_is_stale_without_outbox(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        initial_outbox = await self.repo.outbox()
        invalid = active()
        invalid["alerts"][0]["location_oblast_uid"] = "must-not-leak" * 500

        self.clock += 15
        await self.worker(
            Transport(response(200, invalid, **{"Last-Modified": MARKER_2}))
        ).run_once()

        snapshot = await self.repo.snapshot_for("student-1")
        self.assertEqual(AlertState.STALE, snapshot.state)
        self.assertEqual(initial_outbox, await self.repo.outbox())
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_invalid_official_metadata_after_active_is_stale_never_clear(self):
        await self.worker(
            Transport(
                response(
                    200,
                    official_envelope(),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).run_once()
        invalid_empty = official_envelope(
            {"alerts": []},
            meta={"last_updated_at": "not-an-official-timestamp"},
        )

        for marker in (MARKER_2, MARKER_3):
            self.clock += 15
            await self.worker(
                Transport(response(200, invalid_empty, **{"Last-Modified": marker}))
            ).run_once()
            self.assertEqual(
                AlertState.STALE,
                (await self.repo.snapshot_for("student-1")).state,
            )

        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_invalid_additional_metadata_after_active_is_stale_never_clear(self):
        await self.worker(
            Transport(
                response(
                    200,
                    official_envelope(),
                    **{"Last-Modified": MARKER_1},
                )
            )
        ).run_once()
        invalid_empty = official_envelope(
            {"alerts": []},
            meta={
                "last_updated_at": META_TIMESTAMP,
                "private_depth": {"two": {"three": {"four": {"five": "invalid"}}}},
            },
        )

        for marker in (MARKER_2, MARKER_3):
            self.clock += 15
            await self.worker(
                Transport(response(200, invalid_empty, **{"Last-Modified": marker}))
            ).run_once()
            self.assertEqual(
                AlertState.STALE,
                (await self.repo.snapshot_for("student-1")).state,
            )

        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_ignored_field_on_non_air_row_cannot_bypass_two_observation_clear(self):
        applicable_alert = active()
        applicable_alert["alerts"][0]["location_oblast_uid"] = "must-not-enter-state"
        no_applicable_alert = active(alert_type="chemical")
        no_applicable_alert["alerts"][0]["location_oblast_uid"] = {"ignored": 1}
        no_applicable_alert["alerts"][0]["undocumented_provider_data"] = {"private": True}
        second_no_applicable_alert = active(alert_type="chemical")
        second_no_applicable_alert["alerts"][0]["location_oblast_uid"] = False
        second_no_applicable_alert["alerts"][0]["undocumented_provider_data"] = {"private": False}
        transport = Transport(
            response(200, applicable_alert, **{"Last-Modified": MARKER_1}),
            response(200, no_applicable_alert, **{"Last-Modified": MARKER_2}),
            response(200, second_no_applicable_alert, **{"Last-Modified": MARKER_3}),
        )
        worker = self.worker(transport)

        await worker.run_once()
        self.clock += 15
        await worker.run_once()
        self.assertEqual(
            AlertState.STALE,
            (await self.repo.snapshot_for("student-1")).state,
        )
        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

        self.clock += 15
        await worker.run_once()
        self.assertEqual(
            AlertState.CLEAR_CONFIRMED,
            (await self.repo.snapshot_for("student-1")).state,
        )
        outbox = await self.repo.outbox()
        self.assertFalse(
            any(
                "undocumented" in repr(item)
                or "private" in repr(item)
                or "must-not-enter-state" in repr(item)
                for item in outbox
            )
        )

    async def test_ignored_field_cannot_suppress_parent_or_child_alerts(self):
        scope = RegionScope.from_hierarchy(
            "1803",
            ancestors=("16",),
            descendants=("1903",),
        )

        for uid, location_type in (("16", "oblast"), ("1903", "hromada")):
            with self.subTest(uid=uid):
                payload = active(uid=uid, location_type=location_type)
                payload["alerts"][0]["location_oblast_uid"] = "deliberately-mismatched"
                payload["alerts"][0]["undocumented_provider_data"] = {"must_not_affect_state": True}
                repository = InMemoryAlertRepository([("student", scope)])
                worker = AlertWorker(
                    provider=adapter(
                        Transport(response(200, payload, **{"Last-Modified": MARKER_1}))
                    ),
                    repository=repository,
                    lease=InMemorySingletonLease(),
                    monotonic=lambda: self.clock,
                    jitter=lambda a, b: 7.0,
                )

                await worker.run_once()

                self.assertEqual(
                    AlertState.ACTIVE,
                    (await repository.snapshot_for("student")).state,
                )

    async def test_duplicate_root_alerts_after_active_never_clear(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()

        for marker in (MARKER_2, MARKER_3):
            self.clock += 15
            await self.worker(
                Transport(raw_response(duplicate_root_alerts_payload(), marker))
            ).run_once()
            self.assertEqual(
                AlertState.STALE,
                (await self.repo.snapshot_for("student-1")).state,
            )

        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_excessively_deep_raw_json_after_active_never_clear(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()

        for marker in (MARKER_2, MARKER_3):
            self.clock += 15
            await self.worker(
                Transport(raw_response(deeply_nested_metadata_payload(), marker))
            ).run_once()
            self.assertEqual(
                AlertState.STALE,
                (await self.repo.snapshot_for("student-1")).state,
            )

        self.assertFalse(
            any(
                item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
                for item in await self.repo.outbox()
            )
        )

    async def test_out_of_range_non_air_id_after_active_never_clears(self):
        await self.worker(
            Transport(response(200, active(), **{"Last-Modified": MARKER_1}))
        ).run_once()
        invalid_non_air = active(
            SIGNED_INT64_MAX + 1,
            alert_type="chemical",
        )
        self.clock += 15
        await self.worker(
            Transport(response(200, invalid_non_air, **{"Last-Modified": MARKER_2}))
        ).run_once()
        self.assertEqual(AlertState.STALE, (await self.repo.snapshot_for("student-1")).state)

        self.clock += 15
        await self.worker(
            Transport(response(200, invalid_non_air, **{"Last-Modified": MARKER_3}))
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
