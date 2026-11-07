"""Head15 support must preserve legacy encrypted restore contracts."""

from scripts.deployment import verified_backup as backup


def test_head15_adds_only_three_history_tables_and_validates_ddl_read_only():
    assert backup.SCHEMA_TABLES[backup.ALERT_HISTORY_SCHEMA] - backup.SCHEMA_TABLES[
        backup.REMINDER_SCHEMA
    ] == {
        "alert_history_cursors",
        "alert_history_periods",
        "alert_history_segments",
    }
    script, marker = backup.RESTORE_INSPECTORS[backup.ALERT_HISTORY_SCHEMA]
    compile(script, "head15-restore-inspector", "exec")
    assert "if heads != ['0015_alert_history']" in script
    assert "history.validate_schema(bind)" in script
    assert "history.down_revision != reminders.revision" in script
    assert "SET TRANSACTION READ ONLY" in script
    assert "command.upgrade" not in script
    assert marker == b"ALERT_HISTORY_SCHEMA_VALIDATED\n"
    assert "if heads != ['0014_calendar_reminders']" in backup.REMINDER_SCHEMA_INSPECTOR
    assert "aad=b'TimeTL/RC24/revision14/0008_homework_foundation'" in backup.CRYPTO
