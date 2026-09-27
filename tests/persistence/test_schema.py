from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from time_tl.infrastructure.db import models
from time_tl.infrastructure.db.base import Base


class PersistenceSchemaTests(unittest.TestCase):
    def test_required_tables_are_present(self):
        required = {
            "users",
            "schedule_profiles",
            "schedule_rules",
            "date_overrides",
            "rule_lessons",
            "override_lessons",
            "pending_import_sessions",
            "share_sessions",
            "alert_poll_state",
            "alert_scopes",
            "alert_snapshots",
            "alert_episodes",
            "outbox_messages",
            "worker_heartbeats",
            "runtime_settings",
            "admin_accounts",
            "admin_sessions",
            "admin_login_attempts",
            "audit_log",
            "daily_aggregate_stats",
        }
        self.assertTrue(required.issubset(Base.metadata.tables))

    def test_schedule_constraints_compile_for_postgres(self):
        sql = str(
            CreateTable(models.ScheduleProfile.__table__).compile(dialect=postgresql.dialect())
        )
        self.assertIn("UNIQUE (user_id, public_id)", sql)
        self.assertIn("Europe/Kyiv", sql)
        indexes = "\n".join(
            str(CreateIndex(i).compile(dialect=postgresql.dialect()))
            for i in models.ScheduleProfile.__table__.indexes
        )
        self.assertIn("WHERE is_active", indexes)

    def test_sensitive_values_are_hash_or_ciphertext_columns(self):
        self.assertNotIn("token", models.PendingImportSession.__table__.c)
        self.assertIn("token_hash", models.PendingImportSession.__table__.c)
        self.assertNotIn("join_url", models.RuleLesson.__table__.c)
        self.assertIn("join_url_ciphertext", models.RuleLesson.__table__.c)

    def test_initial_metadata_admin_session_parity(self):
        columns = set(models.AdminSession.__table__.c.keys())
        self.assertIn("csrf_token", columns)
        self.assertNotIn("csrf_token_hash", columns)
        ddl = str(CreateTable(models.AdminSession.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("csrf_token VARCHAR(128)", ddl)

    def test_initial_metadata_user_account_parity(self):
        columns = set(models.User.__table__.c.keys())
        self.assertTrue(
            {"confirmed_region_id", "region_name", "reminder_lead_minutes"}.issubset(columns)
        )
        ddl = str(CreateTable(models.User.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("region_name VARCHAR(160)", ddl)
        self.assertIn("reminder_lead_minutes INTEGER", ddl)

    def test_alert_episode_has_full_identity_primary_key(self):
        self.assertEqual(
            {c.name for c in models.AlertEpisode.__table__.primary_key.columns},
            {"scope_id", "location_uid", "alert_type", "episode_id"},
        )

    def test_outbox_dedupe_and_bounded_scan_indexes(self):
        self.assertTrue(models.OutboxMessage.__table__.c.event_key.unique)
        indexes = {item.name: item for item in models.OutboxMessage.__table__.indexes}
        self.assertTrue(
            {
                "ix_outbox_pending",
                "ix_outbox_aggregate",
                "ix_outbox_confirmed_clear_cursor",
                "ix_outbox_delivered_retention",
                "ix_outbox_dead_retention",
            }.issubset(indexes)
        )
        aggregate_sql = str(
            CreateIndex(indexes["ix_outbox_aggregate"]).compile(dialect=postgresql.dialect())
        )
        clear_sql = str(
            CreateIndex(indexes["ix_outbox_confirmed_clear_cursor"]).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("(aggregate_type, aggregate_id)", aggregate_sql)
        self.assertIn("(created_at, id)", clear_sql)
        self.assertIn("WHERE event_type = 'ALL_CLEAR_CONFIRMED'", clear_sql)
        delivered_sql = str(
            CreateIndex(indexes["ix_outbox_delivered_retention"]).compile(
                dialect=postgresql.dialect()
            )
        )
        dead_sql = str(
            CreateIndex(indexes["ix_outbox_dead_retention"]).compile(dialect=postgresql.dialect())
        )
        self.assertIn("(delivered_at, id)", delivered_sql)
        self.assertIn("claimed_at IS NULL", delivered_sql)
        self.assertIn("(dead_at, id)", dead_sql)
        self.assertIn("claimed_at IS NULL", dead_sql)

    def test_pending_import_capacity_index_is_partial_and_user_ordered(self):
        indexes = {item.name: item for item in models.PendingImportSession.__table__.indexes}
        sql = str(
            CreateIndex(indexes["ix_pending_import_active_user_created"]).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("(user_id, created_at, token_hash)", sql)
        self.assertIn("WHERE revoked_at IS NULL AND consumed_at IS NULL", sql)

    def test_current_metadata_migrations_are_noops_in_complete_offline_script(self):
        for module_name in (
            "migrations.versions.0004_territory_catalog",
            "migrations.versions.0005_public_data_indexes",
            "migrations.versions.0006_outbox_terminal_retention",
        ):
            migration = importlib.import_module(module_name)
            with (
                patch.object(migration.context, "is_offline_mode", return_value=True),
                patch.object(migration, "op") as operations,
                patch.object(migration.sa, "inspect") as inspect,
            ):
                migration.upgrade()
                migration.downgrade()
            self.assertEqual([], operations.mock_calls)
            inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
