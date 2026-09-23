import copy
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from time_tl.domain.schedule import (
    ConfirmationError,
    InMemoryImportSessionRepository,
    Lesson,
    ProfileCollection,
    ProfileLimitReached,
    ScheduleProfile,
    ScheduleRule,
    SnapshotError,
    confirm_import,
    confirm_import_replacement,
    encode_snapshot,
    export_snapshot,
    propose_import,
    request_import_replacement,
)


def profile(profile_id, *, active=False, url="https://secret.invalid/class"):
    item = Lesson(time(9), time(9, 45), "Math", join_url=url)
    return ScheduleProfile(
        profile_id, f"Profile {profile_id}", (ScheduleRule(0, (item,)),), active=active
    )


class ProfileCollectionTests(unittest.TestCase):
    def test_non_empty_collection_requires_exactly_one_active(self):
        with self.assertRaisesRegex(ValueError, "exactly one active"):
            ProfileCollection((profile("1"),))
        with self.assertRaisesRegex(ValueError, "exactly one active"):
            ProfileCollection((profile("1", active=True), profile("2", active=True)))

    def test_first_normal_profile_becomes_active_and_limit_is_three(self):
        collection = ProfileCollection()
        collection.add(profile("1"))
        collection.add(profile("2"))
        collection.add(profile("3"))
        self.assertEqual(collection.active_profile.profile_id, "1")
        with self.assertRaises(ProfileLimitReached):
            collection.add(profile("4"))

    def test_activation_requires_current_confirmation_proposal_and_rejects_replay(self):
        collection = ProfileCollection((profile("1", active=True), profile("2")))
        proposal = collection.request_activation("2", start=date(2026, 9, 21))
        self.assertEqual(len(proposal.resolved_preview), 14)
        with self.assertRaises(ConfirmationError):
            collection.confirm_activation(proposal, confirmed=False)
        activated = collection.confirm_activation(proposal, confirmed=True)
        self.assertEqual(activated.profile_id, "2")
        self.assertEqual(sum(item.active for item in collection.profiles), 1)
        with self.assertRaises(ConfirmationError):
            collection.confirm_activation(proposal, confirmed=True)


class AcceptVerifier:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.payload = None

    def verify(self, payload, integrity):
        self.payload = payload
        return self.accepted and integrity.get("signature") == "valid"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.original = profile("owner", active=True)
        self.created = datetime(2026, 9, 20, tzinfo=UTC)
        self.snapshot = export_snapshot(
            self.original,
            source_display_label="Class 7-A",
            created_at=self.created,
        )

    def propose(self, snapshot=None, sessions=None, **kwargs):
        sessions = sessions or InMemoryImportSessionRepository()
        proposal = propose_import(
            snapshot or self.snapshot,
            sessions=sessions,
            start=date(2026, 9, 21),
            **kwargs,
        )
        return sessions, proposal

    def test_default_export_excludes_sensitive_and_identity_fields(self):
        encoded = encode_snapshot(self.snapshot)
        self.assertNotIn("join_url", encoded)
        self.assertNotIn("https://secret.invalid", encoded)
        for prohibited in ("profile_id", "active", "telegram", "region", "coordinates", "alert"):
            self.assertNotIn(prohibited, encoded.lower())

    def test_unconfirmed_import_does_not_mutate_profile_collection(self):
        collection = ProfileCollection()
        sessions, proposal = self.propose()
        self.assertEqual(collection.profiles, ())
        with self.assertRaises(ConfirmationError):
            confirm_import(collection, proposal, sessions=sessions, confirmed=False)
        self.assertEqual(collection.profiles, ())
        self.assertFalse(sessions.get(proposal.token).consumed)

    def test_payload_links_override_false_metadata_and_need_separate_confirmation(self):
        snapshot = export_snapshot(
            self.original,
            source_display_label="Class",
            created_at=self.created,
            include_join_links=True,
            confirm_include_join_links=True,
        )
        snapshot["metadata"]["included_fields"] = []
        sessions, proposal = self.propose(snapshot)
        self.assertTrue(proposal.links_present)
        self.assertIn("join_links", proposal.included_fields)
        with self.assertRaises(ConfirmationError):
            confirm_import(ProfileCollection(), proposal, sessions=sessions, confirmed=True)
        imported = confirm_import(
            ProfileCollection(),
            proposal,
            sessions=sessions,
            confirmed=True,
            confirm_include_join_links=True,
        )
        self.assertEqual(imported.rules[0].lessons[0].join_url, "https://secret.invalid/class")

    def test_first_import_becomes_active_and_existing_active_is_preserved(self):
        empty = ProfileCollection()
        sessions, first = self.propose()
        imported_first = confirm_import(empty, first, sessions=sessions, confirmed=True)
        self.assertTrue(imported_first.active)

        existing = ProfileCollection((profile("existing", active=True),))
        sessions, second = self.propose()
        imported_second = confirm_import(existing, second, sessions=sessions, confirmed=True)
        self.assertFalse(imported_second.active)
        self.assertEqual(existing.active_profile.profile_id, "existing")

    def test_token_is_bound_one_time_and_independent(self):
        collection = ProfileCollection()
        sessions, proposal = self.propose()
        forged = replace(proposal, profile=profile("forged"))
        with self.assertRaises(ConfirmationError):
            confirm_import(collection, forged, sessions=sessions, confirmed=True)
        self.snapshot["profile"]["name"] = "Changed later"
        imported = confirm_import(collection, proposal, sessions=sessions, confirmed=True)
        self.assertEqual(imported.name, "Profile owner")
        with self.assertRaises(ConfirmationError):
            confirm_import(collection, proposal, sessions=sessions, confirmed=True)

    def test_explicit_replacement_is_atomic_and_one_time(self):
        collection = ProfileCollection((profile("1", active=True), profile("2"), profile("3")))
        sessions, proposal = self.propose()
        with self.assertRaises(ProfileLimitReached):
            confirm_import(collection, proposal, sessions=sessions, confirmed=True)
        replacement = request_import_replacement(
            collection,
            proposal,
            sessions=sessions,
            replace_profile_id="2",
        )
        with self.assertRaises(ConfirmationError):
            confirm_import_replacement(collection, replacement, sessions=sessions, confirmed=False)
        self.assertEqual([item.profile_id for item in collection.profiles], ["1", "2", "3"])
        stored = confirm_import_replacement(
            collection,
            replacement,
            sessions=sessions,
            confirmed=True,
        )
        self.assertFalse(stored.active)
        self.assertEqual(collection.active_profile.profile_id, "1")
        with self.assertRaises(ConfirmationError):
            confirm_import_replacement(collection, replacement, sessions=sessions, confirmed=True)

    def test_confirm_rechecks_expiry_and_revocation(self):
        snapshot = export_snapshot(
            self.original,
            source_display_label="Class",
            created_at=self.created,
            expires_at=self.created + timedelta(hours=1),
        )
        sessions, proposal = self.propose(snapshot, now=self.created + timedelta(minutes=30))
        collection = ProfileCollection()
        with self.assertRaisesRegex(ConfirmationError, "expired"):
            confirm_import(
                collection,
                proposal,
                sessions=sessions,
                confirmed=True,
                now=self.created + timedelta(hours=2),
            )
        self.assertEqual(collection.profiles, ())

        sessions, proposal = self.propose()
        sessions.revoke(proposal.token)
        with self.assertRaisesRegex(ConfirmationError, "revoked"):
            confirm_import(ProfileCollection(), proposal, sessions=sessions, confirmed=True)

    def test_signed_snapshot_requires_verifier(self):
        snapshot = export_snapshot(
            self.original,
            source_display_label="Class",
            created_at=self.created,
            integrity={"algorithm": "test", "signature": "valid"},
        )
        with self.assertRaises(SnapshotError):
            propose_import(snapshot, sessions=InMemoryImportSessionRepository())
        verifier = AcceptVerifier(True)
        sessions = InMemoryImportSessionRepository()
        proposal = propose_import(snapshot, sessions=sessions, verifier=verifier)
        self.assertNotIn(b'"integrity"', verifier.payload)
        self.assertTrue(proposal.requires_confirmation)

    def test_metadata_allowlist_and_recursive_sensitive_key_rejection(self):
        extra_metadata = copy.deepcopy(self.snapshot)
        extra_metadata["metadata"]["owner"] = "hidden"
        with self.assertRaisesRegex(SnapshotError, "metadata"):
            propose_import(extra_metadata, sessions=InMemoryImportSessionRepository())

        nested_sensitive = copy.deepcopy(self.snapshot)
        nested_sensitive["integrity"] = {
            "algorithm": "test",
            "signature": "valid",
            "details": {"region_id": "x"},
        }
        with self.assertRaisesRegex(SnapshotError, "prohibited field"):
            propose_import(
                nested_sensitive,
                sessions=InMemoryImportSessionRepository(),
                verifier=AcceptVerifier(True),
            )

    def test_unknown_version_and_prohibited_identity_fail(self):
        bad_version = copy.deepcopy(self.snapshot)
        bad_version["schema_version"] = "2.0"
        with self.assertRaises(SnapshotError):
            propose_import(bad_version, sessions=InMemoryImportSessionRepository())
        prohibited = copy.deepcopy(self.snapshot)
        prohibited["profile"]["telegram_user_id"] = 123
        with self.assertRaisesRegex(SnapshotError, "prohibited field"):
            propose_import(prohibited, sessions=InMemoryImportSessionRepository())


if __name__ == "__main__":
    unittest.main()
