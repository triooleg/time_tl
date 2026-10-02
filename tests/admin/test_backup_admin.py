from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.backups import BackupCatalogue, BackupCatalogueUnavailable, public_control
from time_tl.admin.security import utcnow
from time_tl.config import Settings
from time_tl.infrastructure.db.admin import BackupControlConflict

ARTIFACT = "a" * 32


def catalogue() -> dict:
    now = utcnow()
    value = {
        "schema": 1,
        "generated_at": now.isoformat(),
        "retention_days": 7,
        "active_revision": 14,
        "application_image": "localhost:5000/time-tl@sha256:" + "b" * 64,
        "backup_source_sha256": "c" * 64,
        "entries": [
            {
                "id": ARTIFACT,
                "snapshot_at": (now - timedelta(hours=1)).isoformat(),
                "verified_at": now.isoformat(),
                "size_bytes": 1200,
                "sha256": "d" * 64,
                "schema": "0008_homework_foundation",
                "restore": "PASS",
            }
        ],
    }
    seal(value)
    return value


def seal(value: dict) -> None:
    payload = {k: v for k, v in value.items() if k not in {"generated_at", "version"}}
    value["version"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class Repository(FakeAsyncRepository):
    def __init__(self):
        super().__init__(must_change=False)
        self.control = {"version": 0, "retention_days": 7, "request": None}
        self.calls = []

    async def get_backup_control(self):
        return self.control

    async def set_backup_policy(self, **kwargs):
        self.check(kwargs)
        self.control.update(
            retention_days=kwargs["retention_days"], version=self.control["version"] + 1
        )

    def check(self, kwargs):
        if kwargs["expected_version"] != self.control["version"]:
            raise BackupControlConflict()
        self.calls.append(kwargs)

    async def request_backup_operation(self, **kwargs):
        self.check(kwargs)
        self.control.update(
            version=self.control["version"] + 1,
            request={**kwargs, "request_id": "e" * 32, "status": "PENDING"},
        )

    async def cancel_backup_operation(self, **kwargs):
        self.check(kwargs)


@pytest.fixture
def panel(tmp_path):
    path = tmp_path / "catalogue.json"
    value = catalogue()
    path.write_text(json.dumps(value), encoding="utf-8")
    repo = Repository()
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@db/time_tl",
        TIME_TL_ENV="local",
        ADMIN_BACKEND="postgres",
        ADMIN_COOKIE_SECURE=False,
    )
    client = TestClient(
        create_async_admin_app(
            settings, repo, FakeMetrics(), backup_catalogue=BackupCatalogue(path)
        )
    )
    import asyncio

    session = asyncio.run(repo.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", session.token)
    return (
        client,
        repo,
        path,
        value,
        {
            "csrf": session.csrf_token,
            "current_password": "bootstrap-password-long",
            "expected_version": "0",
            "catalogue_version": value["version"],
            "artifact_id": ARTIFACT,
        },
    )


def test_owner_page_and_safe_get(panel):
    client, repo, _, _, _ = panel
    response = client.get("/backups")
    assert response.status_code == 200
    assert "Подготовить восстановление" in response.text
    assert "требуется оператор" in response.text
    assert "Киев" in response.text
    assert client.get("/api/backups").status_code == 200
    assert repo.calls == []
    client.cookies.clear()
    assert client.get("/api/backups").status_code == 401


def test_forced_change(panel):
    client, repo, _, _, data = panel
    repo.owner.must_change_password = True
    assert client.get("/api/backups").status_code == 401
    assert client.post("/backups/verify", data=data, follow_redirects=False).status_code == 303
    assert repo.calls == []


def test_csrf_password_and_lockout(panel):
    client, repo, _, _, data = panel
    assert client.post("/backups/verify", data={**data, "csrf": "bad"}).status_code == 403
    for _ in range(4):
        assert (
            client.post("/backups/verify", data={**data, "current_password": "bad"}).status_code
            == 401
        )
    assert (
        client.post("/backups/verify", data={**data, "current_password": "bad"}).status_code == 429
    )
    assert client.post("/backups/verify", data=data).status_code == 429
    assert repo.calls == []


@pytest.mark.parametrize("days", ["0", "91", "-1", "1.5", "seven"])
def test_retention_bounds(panel, days):
    client, repo, _, _, data = panel
    assert (
        client.post(
            "/settings/backups", data={**data, "retention_days": days, "cleanup_ack": "yes"}
        ).status_code
        == 422
    )
    assert repo.calls == []


def test_policy_ack_and_cas(panel):
    client, repo, _, _, data = panel
    assert client.post("/settings/backups", data={**data, "retention_days": "7"}).status_code == 422
    assert (
        client.post(
            "/settings/backups",
            data={**data, "retention_days": "7", "cleanup_ack": "yes"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/settings/backups", data={**data, "retention_days": "8", "cleanup_ack": "yes"}
        ).status_code
        == 409
    )
    assert repo.control["retention_days"] == 7


def test_restore_is_intent_only(panel):
    client, repo, _, _, data = panel
    assert client.post("/backups/restore-request", data=data).status_code == 422
    body = {**data, "restore_ack": f"ВОССТАНОВИТЬ {ARTIFACT}", "data_loss_ack": "yes"}
    assert (
        client.post("/backups/restore-request", data=body, follow_redirects=False).status_code
        == 303
    )
    assert repo.calls[0]["kind"] == "restore"
    assert repo.calls[0]["expected_active_revision"] == 14
    assert len(repo.calls) == 1
    assert "Отменить запрос" in client.get("/backups").text
    assert (
        client.post(
            "/backups/cancel",
            data={**data, "expected_version": "1", "request_id": "e" * 32},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_unknown_id_and_catalogue_cas(panel):
    client, repo, _, _, data = panel
    for identifier in ("../etc/passwd", "<script>alert(1)</script>", "f" * 32):
        response = client.post("/backups/verify", data={**data, "artifact_id": identifier})
        assert response.status_code == 422
        assert identifier not in response.text
    assert (
        client.post("/backups/verify", data={**data, "catalogue_version": "0" * 64}).status_code
        == 409
    )
    assert client.post("/backups/verify", data=data, follow_redirects=False).status_code == 303
    assert len(repo.calls) == 1


@pytest.mark.parametrize(
    "change", ["stale", "future", "duplicate", "secret", "schema", "sha", "size", "xss"]
)
def test_bad_catalogue(panel, change):
    client, repo, path, value, data = panel
    if change == "stale":
        value["generated_at"] = (utcnow() - timedelta(minutes=6)).isoformat()
    elif change == "future":
        value["generated_at"] = (utcnow() + timedelta(seconds=1)).isoformat()
    elif change == "duplicate":
        value["entries"].append(value["entries"][0])
    elif change == "secret":
        value["entries"][0]["private_path"] = "/secret"
    elif change == "schema":
        value["schema"] = True
    elif change == "sha":
        value["entries"][0]["sha256"] = "invalid"
    elif change == "size":
        value["entries"][0]["size_bytes"] = -1
    elif change == "xss":
        value["entries"][0]["schema"] = "<script>"
    seal(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert client.get("/api/backups").status_code == 503
    assert client.post("/backups/verify", data=data).status_code == 503
    assert repo.calls == []


def test_duplicate_json_keys_and_oversize(tmp_path):
    path = tmp_path / "catalogue.json"
    for text in ('{"schema":1,"schema":1}', " " * 262145):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(BackupCatalogueUnavailable):
            BackupCatalogue(path).read(utcnow())


def test_actual_host_producer_compatible(tmp_path):
    from scripts.deployment.backup_control_host import catalogue as host_catalogue

    entry = catalogue()["entries"][0]
    produced = host_catalogue(
        {"retention_days": 7}, {ARTIFACT: (tmp_path / "opaque", entry)}, utcnow()
    )
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(produced), encoding="utf-8")
    assert BackupCatalogue(path).read(utcnow()) == produced


def test_control_secret_filter():
    result = public_control(
        {
            "version": 0,
            "retention_days": 7,
            "token": "private",
            "request": {
                "kind": "restore",
                "artifact_id": ARTIFACT,
                "request_id": "e" * 32,
                "status": "REQUIRES_OPERATOR",
                "result": {"private": "secret"},
                "actor": "private",
            },
        }
    )
    assert "private" not in json.dumps(result)


def test_form_bounds_and_duplicate_fields(panel):
    client, repo, _, _, _ = panel
    assert client.post("/backups/verify", content="x=" + "a" * 8192).status_code == 413
    assert (
        client.post("/backups/verify", content="&".join(f"x{i}=1" for i in range(17))).status_code
        == 422
    )
    assert client.post("/backups/verify", content="csrf=a&csrf=b").status_code == 422
    assert client.post("/backups/verify", content=b"\xff").status_code == 422
    assert repo.calls == []


def test_symlink_reject(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps(catalogue()), encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink permission unavailable on this workstation")
    with pytest.raises(BackupCatalogueUnavailable):
        BackupCatalogue(link).read(utcnow())
