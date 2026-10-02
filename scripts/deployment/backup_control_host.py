"""Bounded host backup catalogue, retention and one-shot request processing.

Never restores production, clears HOLD or starts application containers. Run every
minute under the SAME dedicated backup flock used by the six-hour backup job.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.deployment import verified_backup as backup

PUBLIC_DIRECTORY = Path("/home/trio/time-tl-releases/rc24-operator-packet")
CATALOGUE = PUBLIC_DIRECTORY / "backup-catalogue.json"
IMAGE = (
    "localhost:5000/time-tl-bot@sha256:"
    "060e2ec4ce1e72cfe4984b6b0e790b14f93ccf82b1498958dd5d334a7c2ae391"
)
ID = re.compile(r"[a-f0-9]{32}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
FILE = re.compile(r"backup-[0-9TZ-]+-([a-f0-9]{32})\.tlbk\Z")
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('time_tl:backup_control')::bigint);"
DEFAULT = {"version": 0, "retention_days": 7, "request": None}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def timestamp(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result > now:
        raise ValueError("Timestamp range")
    return result


def control(value: Any) -> dict[str, Any]:
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError("Control schema")
    if type(value["version"]) is not int or not 0 <= value["version"] <= 2**31 - 1:
        raise ValueError("Control version")
    if type(value["retention_days"]) is not int or not 1 <= value["retention_days"] <= 90:
        raise ValueError("Retention policy")
    request = value["request"]
    if request is not None:
        request_fields = {
            "request_id",
            "kind",
            "artifact_id",
            "catalogue_version",
            "expected_active_revision",
            "expected_image",
            "requested_at",
            "expires_at",
            "status",
            "result",
            "claimed_at",
            "finished_at",
        }
        if not isinstance(request, dict) or set(request) != request_fields:
            raise ValueError("Request schema")
        if any(
            not isinstance(request[key], str) or not ID.fullmatch(request[key])
            for key in ("request_id", "artifact_id")
        ):
            raise ValueError("Request ID")
        if (
            request["kind"] not in {"verify", "restore"}
            or request["status"]
            not in {"PENDING", "CLAIMED", "PASS", "REJECTED", "REQUIRES_OPERATOR"}
            or not isinstance(request["catalogue_version"], str)
            or not HEX.fullmatch(request["catalogue_version"])
            or type(request["expected_active_revision"]) is not int
            or request["expected_active_revision"] <= 0
            or not isinstance(request["expected_image"], str)
            or not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", request["expected_image"])
            or not isinstance(request["result"], dict)
        ):
            raise ValueError("Request validation")
        requested = datetime.fromisoformat(request["requested_at"])
        expires = datetime.fromisoformat(request["expires_at"])
        if (
            requested.tzinfo is None
            or expires.tzinfo is None
            or expires - requested != timedelta(minutes=15)
        ):
            raise ValueError("Request expiry")
        for key in ("claimed_at", "finished_at"):
            if request[key] is not None:
                moment = datetime.fromisoformat(request[key])
                if moment.tzinfo is None or moment < requested:
                    raise ValueError("Request progression")
        claimed, finished = request["claimed_at"], request["finished_at"]
        status = request["status"]
        if (
            (status == "PENDING" and (claimed is not None or finished is not None))
            or (status == "CLAIMED" and (claimed is None or finished is not None))
            or (status in {"PASS", "REQUIRES_OPERATOR"} and (claimed is None or finished is None))
            or (status == "REJECTED" and finished is None)
            or (request["kind"] == "verify" and status == "REQUIRES_OPERATOR")
            or (request["kind"] == "restore" and status == "PASS")
            or (claimed is not None and datetime.fromisoformat(claimed) >= expires)
            or (
                claimed is not None
                and finished is not None
                and datetime.fromisoformat(finished) < datetime.fromisoformat(claimed)
            )
        ):
            raise ValueError("Request lifecycle")
        result_fields = {
            "artifact_sha256",
            "verified_at",
            "schema",
            "size_bytes",
            "table_count",
            "reason_code",
        }
        if not set(request["result"]) <= result_fields:
            raise ValueError("Result fields")
        result = request["result"]
        if "reason_code" in result and result["reason_code"] not in {
            "VERIFY_PASSED",
            "VERIFY_REJECTED",
            "RESTORE_REQUIRES_OPERATOR",
            "EXPIRED",
            "CANCELLED",
            "IDENTITY_CHANGED",
            "CATALOGUE_CHANGED",
            "ARTIFACT_INVALID",
            "INTERNAL_ERROR",
        }:
            raise ValueError("Result reason")
        if "artifact_sha256" in result and (
            not isinstance(result["artifact_sha256"], str)
            or not HEX.fullmatch(result["artifact_sha256"])
        ):
            raise ValueError("Result digest")
        if "schema" in result and (
            not isinstance(result["schema"], str)
            or not re.fullmatch(r"[0-9]{4}_[a-z0-9_]{1,80}", result["schema"])
        ):
            raise ValueError("Result schema")
        for key, maximum in (("size_bytes", 2**63 - 1), ("table_count", 10000)):
            if key in result and (type(result[key]) is not int or not 0 <= result[key] <= maximum):
                raise ValueError("Result integer")
        if "verified_at" in result:
            moment = datetime.fromisoformat(result["verified_at"])
            if moment.tzinfo is None:
                raise ValueError("Result timestamp")
    return value


class DatabaseSession:
    """A persistent local transaction keeps the shared advisory lock during deletion."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                backup.PG,
                "psql",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "time_tl",
                "-d",
                "time_tl",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.lines: queue.Queue[str | None] = queue.Queue(maxsize=16)

        def reader() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if len(line) > 65536:
                    self.process.kill()
                    break
                self.lines.put(line.rstrip("\n"))
            self.lines.put(None)

        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()

    def sql(self, sql: str) -> list[str]:
        assert self.process.stdin is not None
        marker = "__BACKUP_TRANSACTION_END__"
        self.process.stdin.write(sql + "\nSELECT '" + marker + "';\n")
        self.process.stdin.flush()
        result = []
        while True:
            try:
                line = self.lines.get(timeout=30)
            except queue.Empty:
                raise TimeoutError("Database transaction deadline") from None
            if line is None:
                raise RuntimeError("Database transaction failed")
            if line == marker:
                return result
            result.append(line)
            if sum(len(item) for item in result) > 65536:
                raise ValueError("Database metadata limit")

    def read(self) -> dict[str, Any]:
        rows = self.sql("SELECT value::text FROM runtime_settings WHERE key='backup_control';")
        return control(
            json.loads(rows[0], object_pairs_hook=backup.monitor.unique) if rows else None
        )

    def compare_and_swap(self, expected: dict[str, Any], value: dict[str, Any]) -> None:
        if self.read() != expected or value["version"] != expected["version"] + 1:
            raise ValueError("Control conflict")
        encoded = base64.b64encode(canonical(value)).decode("ascii")
        rows = self.sql(
            "UPDATE runtime_settings SET updated_at=now(),updated_by='backup-host',"
            "value=convert_from(decode('"
            + encoded
            + "','base64'),'UTF8')::jsonb WHERE key='backup_control' "
            "AND (value->>'version')::bigint=" + str(expected["version"]) + " RETURNING key;"
        )
        if rows != ["backup_control"]:
            raise ValueError("Control conflict")
        request = value["request"]
        details = {
            "version": value["version"],
            "retention_days": value["retention_days"],
            "kind": request["kind"],
            "status": request["status"],
        }
        encoded_details = base64.b64encode(canonical(details)).decode("ascii")
        action = "backup_claimed" if request["status"] == "CLAIMED" else "backup_finished"
        self.sql(
            "INSERT INTO audit_log (actor,action,result,target_type,redacted_details) "
            "VALUES ('backup-host','" + action + "','success','backup_control',"
            "convert_from(decode('" + encoded_details + "','base64'),'UTF8')::jsonb);"
        )

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()


@contextmanager
def transaction() -> Iterator[DatabaseSession]:
    session = DatabaseSession()
    try:
        session.sql(
            "BEGIN; SET LOCAL lock_timeout='10s'; SET LOCAL statement_timeout='30s'; " + LOCK_SQL
        )
        yield session
        session.sql("COMMIT;")
    finally:
        session.close()


def records(now: datetime) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Reject/ignore untrusted files; only successful paired receipts enter catalogue."""
    backup.secure_directory(backup.DIRECTORY)
    result = {}
    duplicate = False
    for path in backup.DIRECTORY.iterdir():
        match = FILE.fullmatch(path.name)
        if match is None:
            continue
        try:
            raw = backup.read_artifact(path)
            receipt = backup.monitor.protected_read(path.with_name(path.name + ".receipt.json"))
            if not isinstance(receipt, dict):
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if (
                receipt.get("status") != "PASS"
                or receipt.get("restore") != "PASS"
                or receipt.get("artifact") != path.name
                or receipt.get("sha256") != digest
                or type(receipt.get("bytes")) is not int
                or receipt["bytes"] != len(raw)
                or receipt.get("schema") not in backup.SCHEMA_TABLES
                or receipt.get("postgres_image_id") != backup.PG_IMAGE
                or receipt.get("tables_checked") != len(backup.SCHEMA_TABLES[receipt["schema"]])
                or receipt.get("constraint_validation") != "PASS"
            ):
                continue
            snapshot = timestamp(receipt["started_at"], now)
            verified = timestamp(receipt["verified_at"], now)
            if snapshot > verified:
                continue
            identifier = match.group(1)
            if identifier in result:
                duplicate = True
                continue
            result[identifier] = (
                path,
                {
                    "id": identifier,
                    "snapshot_at": snapshot.isoformat(),
                    "verified_at": verified.isoformat(),
                    "size_bytes": len(raw),
                    "sha256": digest,
                    "schema": receipt["schema"],
                    "restore": "PASS",
                },
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if duplicate or len(result) > 500:
        raise ValueError("Catalogue capacity")
    return result


def catalogue(
    value: dict[str, Any], entries: dict[str, tuple[Path, dict[str, Any]]], now: datetime
) -> dict[str, Any]:
    payload = {
        "schema": 1,
        "retention_days": value["retention_days"],
        "active_revision": 14,
        "application_image": IMAGE,
        "backup_source_sha256": hashlib.sha256(Path(backup.__file__).read_bytes()).hexdigest(),
        "entries": sorted(
            (entry for _, entry in entries.values()),
            key=lambda entry: (entry["snapshot_at"], entry["id"]),
            reverse=True,
        ),
    }
    return dict(
        payload,
        generated_at=now.isoformat(),
        version=hashlib.sha256(canonical(payload)).hexdigest(),
    )


def publish(value: dict[str, Any]) -> None:
    for path in (PUBLIC_DIRECTORY, *PUBLIC_DIRECTORY.parents):
        if path.is_symlink():
            raise ValueError("Public catalogue directory symlink")
    info = PUBLIC_DIRECTORY.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("Public catalogue ownership")
    if CATALOGUE.exists() or CATALOGUE.is_symlink():
        existing = CATALOGUE.lstat()
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) != 0o444
        ):
            raise ValueError("Public catalogue protection")
    raw = canonical(value)
    if len(raw) > 262144:
        raise ValueError("Public catalogue size")
    fd, temporary = tempfile.mkstemp(prefix=".backup-catalogue-", dir=PUBLIC_DIRECTORY)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        os.replace(temporary, CATALOGUE)
        fsync_directory(PUBLIC_DIRECTORY)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def retention(now: datetime) -> dict[str, Any]:
    """Called ONLY after this backup cycle has successfully verified a new copy."""
    known = records(now)
    if not known:
        return {"deleted": 0}
    with transaction() as session:
        value = session.read()
        selected = value.get("request") or {}
        protected = {max(known, key=lambda key: known[key][1]["snapshot_at"])}
        if selected.get("status") in {"PENDING", "CLAIMED", "REQUIRES_OPERATOR"}:
            if not isinstance(selected.get("artifact_id"), str) or not ID.fullmatch(
                selected["artifact_id"]
            ):
                raise ValueError("Protected selection malformed")
            protected.add(selected["artifact_id"])
        cutoff = now - timedelta(days=value["retention_days"])
        deleted = 0
        for identifier, (path, entry) in known.items():
            if identifier in protected or timestamp(entry["snapshot_at"], now) >= cutoff:
                continue
            # Revalidate immediately under the shared DB lock and global backup flock.
            current_raw = backup.read_artifact(path)
            current_receipt = backup.monitor.protected_read(
                path.with_name(path.name + ".receipt.json")
            )
            if (
                hashlib.sha256(current_raw).hexdigest() != entry["sha256"]
                or current_receipt.get("sha256") != entry["sha256"]
                or current_receipt.get("status") != "PASS"
                or current_receipt.get("restore") != "PASS"
                or current_receipt.get("artifact") != path.name
            ):
                raise ValueError("Retention artefact drift")
            receipt = path.with_name(path.name + ".receipt.json")
            receipt.unlink()
            path.unlink()
            deleted += 1
        fsync_directory(backup.DIRECTORY)
        # Publish immediately after deletion while the policy/selection lock is
        # still held, rather than serving a removed entry for the next minute.
        publish(catalogue(value, records(now), now))
    return {"deleted": deleted, "retention_days": value["retention_days"]}


def active_identity(session: DatabaseSession) -> bool:
    rows = session.sql(
        "SELECT COALESCE((SELECT value->>'active_revision' FROM runtime_settings "
        "WHERE key='owner_configuration_state'),'') || ':' || "
        "COALESCE((SELECT value->>'revision' FROM runtime_settings "
        "WHERE key='owner_configuration_active'),'');"
    )
    return rows == ["14:14"]


def changed_request(
    value: dict[str, Any], status: str, result: dict[str, Any], now: datetime
) -> dict[str, Any]:
    updated = json.loads(json.dumps(value))
    updated["version"] += 1
    request = updated["request"]
    request["status"] = status
    request["result"] = result
    request["claimed_at" if status == "CLAIMED" else "finished_at"] = now.isoformat()
    return control(updated)


def process_request(known: dict[str, tuple[Path, dict[str, Any]]], now: datetime) -> str:
    """Claim once with the shared CAS lock; verify outside DB transaction."""
    with transaction() as session:
        value = session.read()
        now = datetime.now(UTC)
        request = value["request"]
        if request is None or request["status"] != "PENDING":
            return "NO_PENDING_REQUEST"
        reason = None
        if (
            datetime.fromisoformat(request["expires_at"]) <= now
            or datetime.fromisoformat(request["requested_at"]) > now
        ):
            reason = "EXPIRED"
        elif (
            request["expected_active_revision"] != 14
            or request["expected_image"] != IMAGE
            or not active_identity(session)
        ):
            reason = "IDENTITY_CHANGED"
        elif request["catalogue_version"] != catalogue(value, known, now)["version"]:
            reason = "CATALOGUE_CHANGED"
        elif request["artifact_id"] not in known:
            reason = "ARTIFACT_INVALID"
        if reason is not None:
            session.compare_and_swap(
                value, changed_request(value, "REJECTED", {"reason_code": reason}, now)
            )
            return "REJECTED"
        updated = changed_request(value, "CLAIMED", {}, now)
        session.compare_and_swap(value, updated)
        claimed = updated["request"]
    path, entry = known[claimed["artifact_id"]]
    status = "REQUIRES_OPERATOR" if claimed["kind"] == "restore" else "PASS"
    result = {
        "artifact_sha256": entry["sha256"],
        "size_bytes": entry["size_bytes"],
        "schema": entry["schema"],
        "reason_code": "RESTORE_REQUIRES_OPERATOR",
    }
    try:
        raw = backup.read_artifact(path)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("Artifact drift")
        image = backup.identity()
        if claimed["kind"] == "verify":
            restored = backup.restore(
                backup.crypt(raw, "decrypt"), image, expected_schema=entry["schema"]
            )
            result.update(
                reason_code="VERIFY_PASSED",
                table_count=restored["tables_checked"],
                verified_at=datetime.now(UTC).isoformat(),
            )
    except Exception:
        status = "REJECTED"
        result = {
            "reason_code": "VERIFY_REJECTED" if claimed["kind"] == "verify" else "ARTIFACT_INVALID"
        }
    with transaction() as session:
        current = session.read()
        current_request = current["request"]
        if (
            current_request is None
            or current_request["request_id"] != claimed["request_id"]
            or current_request["status"] != "CLAIMED"
        ):
            raise ValueError("Request finish conflict")
        if not active_identity(session):
            status, result = "REJECTED", {"reason_code": "IDENTITY_CHANGED"}
        session.compare_and_swap(
            current, changed_request(current, status, result, datetime.now(UTC))
        )
    return status


def poll() -> dict[str, Any]:
    now = datetime.now(UTC)
    backup.identity()
    known = records(now)
    with transaction() as session:
        value = session.read()
        public = catalogue(value, known, now)
    publish(public)
    request_status = process_request(known, now)
    return {
        "status": "PASS",
        "catalogue_version": public["version"],
        "entries": len(known),
        "retention_days": value["retention_days"],
        "production_restored": False,
        "request_status": request_status,
    }


def main() -> int:
    try:
        print(json.dumps(poll(), sort_keys=True))
        return 0
    except Exception:
        print('{"status":"REJECTED","classification":"backup_control_failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
