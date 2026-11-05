"""Frozen restore contract for the additive power schema; no Docker or secrets."""

from scripts.deployment import verified_backup as backup


def test_power_restore_preserves_calendar_validation_and_adds_frozen_power_checks():
    assert backup.SCHEMA_TABLES[backup.POWER_SCHEMA] - backup.SCHEMA_TABLES[
        backup.PERSONAL_SCHEMA
    ] == {
        "power_providers",
        "power_schedule_snapshots",
        "power_schedule_intervals",
        "user_power_subscriptions",
        "power_notification_events",
    }
    script = backup.POWER_SCHEMA_INSPECTOR
    compile(script, "power-restore-inspector", "exec")
    assert "if heads != ['0011_power_outages']" in script
    assert "prior._validate(inspect(bind),prior._schema())" in script
    assert "personal._validate(personal._schema())" in script
    assert "power.validate_schema(bind)" in script
    assert "SET TRANSACTION READ ONLY" in script
    assert "POWER_SCHEMA_VALIDATED" in script
    assert "command.upgrade" not in script


def test_legacy_encryption_format_is_unchanged():
    assert "aad=b'TimeTL/RC24/revision14/0008_homework_foundation'" in backup.CRYPTO
    assert "TimeTL/RC24/backup/v1" in backup.CRYPTO
