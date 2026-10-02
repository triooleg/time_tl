"""Local guard/receipt tests. These do not claim the PostgreSQL lab ran."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.validation import calendar_postgres_lab as lab

SAFE = "postgresql+asyncpg://calendar_lab@time-tl-calendar-lab-pg/calendar_lab"


def test_only_exact_trust_only_named_disposable_target_allowed():
    assert lab.guard_database_url(SAFE) == SAFE
    assert lab.guard_database_url(SAFE.replace("-pg/", "-pg:5432/")) == SAFE.replace(
        "-pg/", "-pg:5432/"
    )


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+asyncpg://calendar_lab@postgres/calendar_lab",
        "postgresql+asyncpg://calendar_lab@127.0.0.1/calendar_lab",
        "postgresql+asyncpg://time_tl@time-tl-calendar-lab-pg/calendar_lab",
        "postgresql+asyncpg://calendar_lab:secret@time-tl-calendar-lab-pg/calendar_lab",
        "postgresql+asyncpg://calendar_lab@time-tl-calendar-lab-pg/time_tl",
        "postgresql+asyncpg://calendar_lab@time-tl-calendar-lab-pg:5433/calendar_lab",
        "postgresql+asyncpg://calendar_lab@time-tl-calendar-lab-pg/calendar_lab?host=postgres",
        "postgresql+asyncpg://calendar_lab@time-tl-calendar-lab-pg/calendar_lab#secret",
        "postgresql://calendar_lab@time-tl-calendar-lab-pg/calendar_lab",
        "sqlite:///:memory:",
        "",
        "unparseable",
    ],
)
def test_forbidden_target_fails_with_safe_fixed_code(value):
    with pytest.raises(lab.LabFailure) as error:
        lab.guard_database_url(value)
    assert str(error.value) == "TARGET_GUARD"
    assert "secret" not in str(error.value)


def test_invalid_target_rejects_before_migration_config_or_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must never reach connection creation")

    monkeypatch.setattr(lab, "create_async_engine", forbidden)
    with pytest.raises(lab.LabFailure, match="TARGET_GUARD"):
        lab.guard_database_url("postgresql+asyncpg://real:secret@production/real")


def test_missing_migration_workspace_not_silently_skipped(tmp_path):
    with pytest.raises(lab.LabFailure, match="MIGRATIONS_UNAVAILABLE"):
        lab.migration_config(tmp_path, SAFE)


def test_actual_repository_workspace_paths_and_lab_url_only():
    workspace = Path(__file__).resolve().parents[2]
    config = lab.migration_config(workspace, SAFE)
    assert config.get_main_option("script_location") == str(workspace / "migrations")
    assert config.get_main_option("sqlalchemy.url") == SAFE


@pytest.mark.parametrize("error", [lab.LabFailure("LAB_NOT_EMPTY"), RuntimeError("secret sql")])
def test_cli_rejection_receipt_is_bounded_and_no_exception_or_url_leak(error, monkeypatch, capsys):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(lab, "run_lab", fail)
    monkeypatch.setattr(sys, "argv", ["lab", "--database-url", SAFE])
    assert lab.main() == 1
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    assert receipt["status"] == "REJECTED"
    assert receipt["reason"] in ("LAB_NOT_EMPTY", "UNEXPECTED_LAB_FAILURE")
    assert "secret" not in output.out and SAFE not in output.out
    assert output.err == "" and len(output.out) < 512


def test_cli_success_receipt_contains_only_bounded_evidence(monkeypatch, capsys):
    async def success(*args, **kwargs):
        return {
            "status": "PASS",
            "head": lab.HEAD,
            "table_count": 31,
            "environment": "DISPOSABLE_POSTGRES_LAB",
            "checks": ["mock_receipt_only"],
        }

    monkeypatch.setattr(lab, "run_lab", success)
    monkeypatch.setattr(sys, "argv", ["lab", "--database-url", SAFE])
    assert lab.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["table_count"] == 31 and receipt["checks"] == ["mock_receipt_only"]


def test_profile_fixture_is_link_free_and_has_no_real_user_data():
    snapshot = lab.profile()
    assert snapshot.name == "Lab lesson"
    assert snapshot.rules[0].lessons[0].join_url is None
