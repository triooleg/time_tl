from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from cryptography.fernet import Fernet

from time_tl.admin.settings import (
    DEFAULT_ONBOARDING_TEXT,
    MAX_ONBOARDING_TEXT_LENGTH,
    OwnerConfigurationUpdate,
    OwnerSettingsBundle,
    OwnerSettingsConflictError,
    OwnerSettingsCryptoError,
    OwnerSettingsService,
    OwnerSettingsValidationError,
    reject_sensitive_generic_settings,
)


class MemoryOwnerSettingsStore:
    def __init__(self) -> None:
        self.bundle = OwnerSettingsBundle(None, None, 0, None, None)

    async def owner_settings_bundle(self) -> OwnerSettingsBundle:
        return self.bundle

    async def save_owner_settings_draft(
        self, payload: dict[str, object], expected_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        if self.bundle.latest_revision != expected_revision:
            raise OwnerSettingsConflictError("conflict")
        revision = expected_revision + 1
        saved = dict(payload)
        saved["revision"] = revision
        self.bundle = OwnerSettingsBundle(
            saved,
            self.bundle.active,
            revision,
            None,
            self.bundle.active_revision,
            actor,
            datetime.now(UTC),
        )
        return self.bundle

    async def request_owner_settings_activation(
        self, expected_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        if self.bundle.latest_revision != expected_revision:
            raise OwnerSettingsConflictError("conflict")
        self.bundle = replace(self.bundle, pending_revision=expected_revision, updated_by=actor)
        return self.bundle

    async def mark_owner_settings_active(
        self,
        revision: int,
        actor: str,
        evidence_id: str,
        evidence_summary: dict[str, int | str] | None = None,
    ) -> OwnerSettingsBundle:
        if self.bundle.pending_revision != revision or self.bundle.draft is None:
            raise OwnerSettingsConflictError("conflict")
        previous_active = self.bundle.active
        self.bundle = OwnerSettingsBundle(
            self.bundle.draft,
            dict(self.bundle.draft),
            revision,
            None,
            revision,
            actor,
            datetime.now(UTC),
            previous_active,
            {
                "revision": revision,
                "result": "passed",
                "evidence_id": evidence_id,
                **(evidence_summary or {}),
            },
        )
        return self.bundle

    async def record_owner_settings_preflight_failure(
        self, revision: int, actor: str, failure_code: str
    ) -> OwnerSettingsBundle:
        self.bundle = replace(
            self.bundle,
            updated_by=actor,
            activation_status={
                "revision": revision,
                "result": "failed",
                "failure_code": failure_code,
            },
        )
        return self.bundle

    async def rollback_owner_settings_active(
        self, expected_active_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        if (
            self.bundle.active_revision != expected_active_revision
            or self.bundle.previous_active is None
            or self.bundle.active is None
        ):
            raise OwnerSettingsConflictError("conflict")
        previous = dict(self.bundle.previous_active)
        current = dict(self.bundle.active)
        revision = int(previous["revision"])
        self.bundle = replace(
            self.bundle,
            active=previous,
            active_revision=revision,
            previous_active=current,
            updated_by=actor,
            updated_at=datetime.now(UTC),
            activation_status={"revision": revision, "result": "rolled_back"},
        )
        return self.bundle


def staging_update(revision: int, **overrides: object) -> OwnerConfigurationUpdate:
    values: dict[str, object] = {
        "expected_revision": revision,
        "environment": "staging",
        "alert_provider": "alerts_in_ua",
        "telegram_transport": "live",
        "poll_interval_seconds": 15,
        "stale_after_seconds": 120,
        "staging_allowlist": (123456,),
        "telegram_token": None,
        "alerts_in_ua_token": None,
    }
    values.update(overrides)
    return OwnerConfigurationUpdate(**values)  # type: ignore[arg-type]


class OwnerSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = MemoryOwnerSettingsStore()
        self.telegram_key = Fernet.generate_key()
        self.alerts_key = Fernet.generate_key()
        self.service = OwnerSettingsService(self.store, self.telegram_key, self.alerts_key)

    async def test_defaults_are_fail_closed_simulation(self) -> None:
        view = await self.service.view()
        self.assertEqual(view.activation_state, "DRAFT")
        self.assertEqual(view.environment, "simulation")
        self.assertEqual(view.telegram_transport, "disabled")
        self.assertFalse(view.telegram_token_configured)

    async def test_tokens_are_encrypted_and_never_returned(self) -> None:
        telegram = "telegram-secret-value"
        alerts = "alerts-secret-value"
        view = await self.service.save(
            staging_update(0, telegram_token=telegram, alerts_in_ua_token=alerts), "admin"
        )
        serialized = repr(self.store.bundle)
        self.assertNotIn(telegram, serialized)
        self.assertNotIn(alerts, serialized)
        self.assertTrue(view.telegram_token_configured)
        self.assertTrue(view.alerts_in_ua_token_configured)
        self.assertNotIn("staging_allowlist", view.public_dict())
        self.assertEqual(view.public_dict()["staging_allowlist_count"], 1)

    async def test_blank_secret_and_allowlist_keep_existing_values(self) -> None:
        await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        before = dict(self.store.bundle.draft or {})
        view = await self.service.save(staging_update(1, staging_allowlist=None), "admin")
        after = self.store.bundle.draft or {}
        self.assertEqual(before["telegram_token_ciphertext"], after["telegram_token_ciphertext"])
        self.assertEqual(before["staging_allowlist"], after["staging_allowlist"])
        self.assertTrue(view.ready_for_activation)

    async def test_save_never_activates_and_request_only_sets_pending(self) -> None:
        view = await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        self.assertEqual(view.activation_state, "DRAFT")
        self.assertIsNone(view.active_revision)
        pending = await self.service.request_activation(1, "admin")
        self.assertEqual(pending.activation_state, "PENDING_ACTIVATION")
        self.assertIsNone(pending.active_revision)

    async def test_incomplete_draft_cannot_request_activation(self) -> None:
        view = await self.service.save(staging_update(0), "admin")
        self.assertFalse(view.ready_for_activation)
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.request_activation(1, "admin")

    async def test_production_rejects_staging_allowlist(self) -> None:
        view = await self.service.save(
            staging_update(
                0,
                environment="production",
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        self.assertFalse(view.ready_for_activation)
        self.assertIn(
            "Для PRODUCTION очистите STAGING allowlist.",
            view.validation_issues,
        )
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.request_activation(1, "admin")

    async def test_wrong_key_fails_closed_without_secret_in_error(self) -> None:
        await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        wrong = OwnerSettingsService(self.store, Fernet.generate_key(), self.alerts_key)
        with self.assertRaises(OwnerSettingsCryptoError) as raised:
            await wrong.view()
        self.assertNotIn("telegram-secret", str(raised.exception))

    async def test_activation_requires_pending_version_and_evidence(self) -> None:
        await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        await self.service.request_activation(1, "admin")
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.mark_active(1, "operator", "")
        active = await self.service.mark_active(1, "operator", "G8-test-evidence")
        self.assertEqual(active.activation_state, "ACTIVE")
        runtime = await self.service.active_runtime_config("staging", "bot")
        self.assertEqual(runtime.revision, 1)
        assert runtime.telegram_token is not None
        self.assertEqual(runtime.telegram_token.get_secret_value(), "telegram-secret")
        self.assertIsNone(runtime.alerts_in_ua_token)
        alerts_runtime = await self.service.active_runtime_config("staging", "alert-worker")
        self.assertIsNone(alerts_runtime.telegram_token)
        assert alerts_runtime.alerts_in_ua_token is not None
        self.assertEqual(alerts_runtime.alerts_in_ua_token.get_secret_value(), "alerts-secret")
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.active_runtime_config("production", "bot")

    async def test_activation_status_and_rollback_restore_previous_active_revision(self) -> None:
        await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
            ),
            "admin",
        )
        await self.service.request_activation(1, "admin")
        first = await self.service.mark_active(
            1,
            "admin",
            "evidence-1",
            {"check_profile": "test", "covered_region_count": 1},
        )
        self.assertEqual("passed", first.last_activation_result)

        await self.service.save(staging_update(1, onboarding_text="new onboarding"), "admin")
        await self.service.request_activation(2, "admin")
        second = await self.service.mark_active(2, "admin", "evidence-2")
        self.assertEqual(1, second.rollback_revision)

        rolled_back = await self.service.rollback(2, "staging", "admin")
        self.assertEqual(1, rolled_back.active_revision)
        self.assertEqual("rolled_back", rolled_back.last_activation_result)
        runtime = await self.service.active_runtime_config("staging", "bot")
        self.assertEqual(1, runtime.revision)

    async def test_onboarding_text_save_load_default_and_backward_compatibility(self) -> None:
        default_view = await self.service.view()
        self.assertEqual(DEFAULT_ONBOARDING_TEXT, default_view.onboarding_text)

        custom = "👋 Коротка інструкція <без HTML>"
        saved = await self.service.save(staging_update(0, onboarding_text=custom), "admin")
        self.assertEqual(custom, saved.onboarding_text)
        self.assertEqual(custom, (self.store.bundle.draft or {})["onboarding_text"])

        preserved = await self.service.save(staging_update(1), "admin")
        self.assertEqual(custom, preserved.onboarding_text)

        reset = await self.service.save(staging_update(2, onboarding_text="   "), "admin")
        self.assertEqual(DEFAULT_ONBOARDING_TEXT, reset.onboarding_text)

        legacy_payload = dict(self.store.bundle.draft or {})
        legacy_payload.pop("onboarding_text", None)
        self.store.bundle = replace(self.store.bundle, draft=legacy_payload)
        legacy = await self.service.view()
        self.assertEqual(DEFAULT_ONBOARDING_TEXT, legacy.onboarding_text)

    async def test_onboarding_text_rejects_oversize_and_control_characters(self) -> None:
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.save(
                staging_update(0, onboarding_text="x" * (MAX_ONBOARDING_TEXT_LENGTH + 1)),
                "admin",
            )
        with self.assertRaises(OwnerSettingsValidationError):
            await self.service.save(staging_update(0, onboarding_text="hello\u0000"), "admin")

    async def test_active_runtime_config_exposes_onboarding_text(self) -> None:
        custom = "Перша інструкція"
        await self.service.save(
            staging_update(
                0,
                telegram_token="telegram-secret",
                alerts_in_ua_token="alerts-secret",
                onboarding_text=custom,
            ),
            "admin",
        )
        await self.service.request_activation(1, "admin")
        await self.service.mark_active(1, "operator", "evidence")
        runtime = await self.service.active_runtime_config("staging", "bot")
        self.assertEqual(custom, runtime.onboarding_text)

    def test_generic_settings_reject_sensitive_names(self) -> None:
        for key in ("telegram_token", "password", "fernet_key", "ciphertext"):
            with self.assertRaises(ValueError):
                reject_sensitive_generic_settings({key: "value"})
        reject_sensitive_generic_settings({"poll": 15, "stale": 120})


if __name__ == "__main__":
    unittest.main()
