"""RC24 colocated encrypted backups: consistent dump, persisted readback, actual restore.

No application restart, secret rotation, or persistent plaintext backup file.
Recovery requires the ORIGINAL owner alerts Fernet key for backup decryption and
the other original production Fernet keys for application data; keep offhost escrow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.deployment import cohost_monitor as monitor

DIRECTORY = Path("/home/trio/time-tl-backups-rc24")
CONFIG = Path("/home/trio/time-tl-monitor-rc24/config.json")
PG = "time-tl-production-postgres-1"
ADMIN = "time-tl-production-admin-web-1"
LIMIT = 64 * 1024 * 1024
MAGIC = b"TLBK1"
SCHEMA = "0008_homework_foundation"
# Frozen migration contracts, not derived from possibly changed ORM metadata.
LEGACY_TABLES = frozenset(
    {
        "alembic_version",
        "users",
        "schedule_profiles",
        "schedule_rules",
        "date_overrides",
        "rule_lessons",
        "override_lessons",
        "pending_import_sessions",
        "share_sessions",
        "schedule_subjects",
        "homework_assignments",
        "homework_user_states",
        "pending_homework_drafts",
        "alert_poll_state",
        "territory_catalog_versions",
        "territories",
        "active_territory_catalogs",
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
)
SCHEMA_TABLES = {
    SCHEMA: LEGACY_TABLES,
    "0009_calendar_foundation": LEGACY_TABLES
    | {
        "calendars",
        "calendar_memberships",
        "calendar_invitations",
    },
}
PERSONAL_SCHEMA = "0010_personal_calendar_overrides"
SCHEMA_TABLES[PERSONAL_SCHEMA] = SCHEMA_TABLES["0009_calendar_foundation"] | {
    "personal_calendar_overrides",
}
POWER_SCHEMA = "0011_power_outages"
SCHEMA_TABLES[POWER_SCHEMA] = SCHEMA_TABLES[PERSONAL_SCHEMA] | {
    "power_providers",
    "power_schedule_snapshots",
    "power_schedule_intervals",
    "user_power_subscriptions",
    "power_notification_events",
}
WEATHER_SCHEMA = "0012_weather_coverage"
SCHEMA_TABLES[WEATHER_SCHEMA] = SCHEMA_TABLES[POWER_SCHEMA] | {
    "weather_locations",
    "weather_forecast_runs",
    "weather_forecast_snapshots",
    "weather_forecast_hours",
    "weather_change_events",
    "user_weather_preferences",
    "weather_notification_deliveries",
}
REMINDER_SCHEMA = "0014_calendar_reminders"
SCHEMA_TABLES[REMINDER_SCHEMA] = SCHEMA_TABLES[WEATHER_SCHEMA]
# TLBK1 AAD is a legacy FORMAT label, not an assertion about restored schema.
# Keep it byte-identical so all retained copies use the existing original key.
# The authenticated dump's actual head and exact named tables are validated below.
PG_IMAGE = "sha256:fff3594bf464bea0f502788874403882d8bbbe618b3250f8649e7e42fd118020"
PERSONAL_SCHEMA_INSPECTOR = r"""
import asyncio, importlib, sys
async def main():
 from alembic.migration import MigrationContext
 from alembic.operations import Operations
 from sqlalchemy import inspect, text
 from sqlalchemy.ext.asyncio import create_async_engine
 from unittest.mock import patch
 prior=importlib.import_module('migrations.versions.0009_calendar_foundation')
 personal=importlib.import_module('migrations.versions.0010_personal_calendar_overrides')
 if (prior.revision, personal.revision, personal.down_revision) != (
   '0009_calendar_foundation','0010_personal_calendar_overrides','0009_calendar_foundation'):
  raise ValueError()
 engine=create_async_engine(
  'postgresql+asyncpg://backupcheck@127.0.0.1/backupcheck', echo=False)
 try:
  async with engine.connect() as connection:
   async with connection.begin():
    await connection.execute(text('SET TRANSACTION READ ONLY'))
    result=await connection.execute(text('SELECT version_num FROM alembic_version'))
    heads=result.scalars().all()
    if heads != ['0010_personal_calendar_overrides']: raise ValueError()
    def validate(bind):
     operations=Operations(MigrationContext.configure(bind))
     with patch.object(prior,'op',operations), patch.object(personal,'op',operations):
      prior._validate(inspect(bind),prior._schema())
      personal._validate(personal._schema())
    await connection.run_sync(validate)
 finally:
  await engine.dispose()
try:
 asyncio.run(main())
except BaseException:
 sys.exit(2)
print('CAL29_SCHEMA_VALIDATED')
"""
# Reuse the exact frozen calendar inspection and additionally validate all power
# tables. This changes neither encryption/AAD nor validation of retained backups.
POWER_SCHEMA_INSPECTOR = (
    PERSONAL_SCHEMA_INSPECTOR.replace(
        "if heads != ['0010_personal_calendar_overrides']: raise ValueError()",
        "if heads != ['0011_power_outages']: raise ValueError()",
    )
    .replace(
        "personal._validate(personal._schema())",
        "personal._validate(personal._schema())\n"
        "     power=importlib.import_module('migrations.versions.0011_power_outages')\n"
        "     if power.down_revision != personal.revision: raise ValueError()\n"
        "     power.validate_schema(bind)",
    )
    .replace("CAL29_SCHEMA_VALIDATED", "POWER_SCHEMA_VALIDATED")
)
WEATHER_SCHEMA_INSPECTOR = (
    POWER_SCHEMA_INSPECTOR.replace(
        "if heads != ['0011_power_outages']: raise ValueError()",
        "if heads != ['0012_weather_coverage']: raise ValueError()",
    )
    .replace(
        "power.validate_schema(bind)",
        "power.validate_schema(bind)\n"
        "     weather=importlib.import_module('migrations.versions.0012_weather_coverage')\n"
        "     if weather.down_revision != power.revision: raise ValueError()\n"
        "     weather.validate_schema(bind)",
    )
    .replace("POWER_SCHEMA_VALIDATED", "WEATHER_SCHEMA_VALIDATED")
)
REMINDER_SCHEMA_INSPECTOR = (
    WEATHER_SCHEMA_INSPECTOR.replace(
        "if heads != ['0012_weather_coverage']: raise ValueError()",
        "if heads != ['0014_calendar_reminders']: raise ValueError()",
    )
    .replace(
        "weather.validate_schema(bind)",
        "weather.validate_schema(bind)\n"
        "     alert=importlib.import_module('migrations.versions.0013_alert_transition_revision')\n"
        "     reminders=importlib.import_module('migrations.versions.0014_calendar_reminders')\n"
        "     if (alert.down_revision, reminders.down_revision) != "
        "(weather.revision, alert.revision):\n"
        "      raise ValueError()\n"
        "     alert.validate_schema(bind)\n"
        "     reminders.validate_schema(bind)",
    )
    .replace("WEATHER_SCHEMA_VALIDATED", "REMINDER_SCHEMA_VALIDATED")
)
RESTORE_INSPECTORS = {
    PERSONAL_SCHEMA: (PERSONAL_SCHEMA_INSPECTOR, b"CAL29_SCHEMA_VALIDATED\n"),
    POWER_SCHEMA: (POWER_SCHEMA_INSPECTOR, b"POWER_SCHEMA_VALIDATED\n"),
    WEATHER_SCHEMA: (WEATHER_SCHEMA_INSPECTOR, b"WEATHER_SCHEMA_VALIDATED\n"),
    REMINDER_SCHEMA: (REMINDER_SCHEMA_INSPECTOR, b"REMINDER_SCHEMA_VALIDATED\n"),
}
CRYPTO = r"""
import os,sys,base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
try:
 raw=sys.stdin.buffer.read(67108898)
 if len(raw)>67108897: raise ValueError()
 seed=base64.urlsafe_b64decode(os.environ['OWNER_ALERTS_FERNET_KEY'])
 if len(seed)!=32: raise ValueError()
 key=HKDF(algorithm=hashes.SHA256(),length=32,salt=b'TimeTL/RC24/backup/v1',
          info=b'postgres-dump-encryption-only').derive(seed)
 aad=b'TimeTL/RC24/revision14/0008_homework_foundation'
 if sys.argv[1]=='encrypt':
  if not raw.startswith(b'PGDMP'): raise ValueError()
  nonce=os.urandom(12)
  result=b'TLBK1'+nonce+AESGCM(key).encrypt(nonce,raw,aad)
 else:
  if not raw.startswith(b'TLBK1') or len(raw)<33: raise ValueError()
  result=AESGCM(key).decrypt(raw[5:17],raw[17:],aad)
  if not result.startswith(b'PGDMP'): raise ValueError()
 sys.stdout.buffer.write(result)
except Exception:
 sys.exit(2)
"""


class GuardPublicationAmbiguous(RuntimeError):
    """An operator must verify the guard after publication and rollback failure."""


def command(
    args: list[str], payload: bytes | None = None, timeout: int = 120, limit: int = LIMIT + 33
) -> bytes:
    """Bound captured stdout before allocation growth; stderr deliberately suppressed."""
    process = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    expired = threading.Event()

    def abort() -> None:
        expired.set()
        process.kill()

    timer = threading.Timer(timeout, abort)
    timer.start()

    def feed() -> None:
        assert process.stdin is not None
        try:
            if payload is not None:
                process.stdin.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        assert process.stdout is not None
        output = process.stdout.read(limit + 1)
        if len(output) > limit:
            process.kill()
            raise ValueError("Output limit")
        code = process.wait(timeout=5)
        if code or expired.is_set():
            raise RuntimeError("Command failed")
        return output
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        writer.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def secure_directory(path: Path) -> None:
    # Resolve every existing ancestor: never traverse a symlink into user data.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Directory symlink")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Directory protection")


def read_artifact(path: Path) -> bytes:
    secure_directory(DIRECTORY)
    if path.parent != DIRECTORY or not re.fullmatch(
        r"backup-[0-9TZ-]+-[a-f0-9]{32}\.tlbk", path.name
    ):
        raise ValueError("Artifact path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > LIMIT + 33
        ):
            raise ValueError("Artifact protection")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(LIMIT + 34)
        if not raw.startswith(MAGIC) or len(raw) < 33 or len(raw) > LIMIT + 33:
            raise ValueError("Artifact format")
        return raw
    finally:
        os.close(fd)


def write_artifact(path: Path, raw: bytes) -> None:
    secure_directory(DIRECTORY)
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=DIRECTORY)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # UUID target, exclusive publication; never overwrite any previous backup.
        os.link(temporary, path)
        directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def crypt(raw: bytes, operation: str) -> bytes:
    return command(["docker", "exec", "-i", ADMIN, "python", "-c", CRYPTO, operation], raw)


def identity() -> str:
    config = monitor.protected_read(CONFIG)
    monitor.validate_config(config)
    metadata = json.loads(command(["docker", "inspect", PG, ADMIN], limit=1024 * 1024))
    postgres, admin = metadata
    if not all(item["State"]["Running"] for item in metadata):
        raise ValueError("Runtime stopped")
    if admin["Config"]["Image"] != monitor.expected_admin_image(config):
        raise ValueError("Application identity")
    image = postgres["Image"]
    if image != PG_IMAGE:
        raise ValueError("PostgreSQL identity")
    return image


def inspector_creators_quiescent(name: str) -> None:
    """Reject own still-live Docker creators; this cannot prove daemon quiescence.

    A timed-out CREATE remains rejected even after best-effort owned cleanup:
    killing/reaping the CLI does not cancel a possible late daemon transaction.
    """
    if not re.fullmatch(r"time-tl-cal29-inspector-[0-9a-f]{32}", name):
        raise ValueError("Restore inspector creator identity")
    count = 0
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        count += 1
        if count > 32768:
            raise ValueError("Restore inspector process bound")
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            with (process / "cmdline").open("rb") as stream:
                raw = stream.read(65537)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if not raw:
            continue
        end = raw.find(b"\0", 0, 4097)
        if end < 0:
            raise ValueError("Restore inspector argv bound")
        executable = raw[:end].rsplit(b"/", 1)[-1]
        if executable not in {b"docker", b"docker-compose"}:
            continue
        if len(raw) > 65536 or not raw.endswith(b"\0"):
            raise ValueError("Restore inspector Docker argv bound")
        args = raw[:-1].decode("utf-8", errors="strict").split("\0")
        if "create" in args and name in args:
            raise ValueError("Restore inspector creator still live")


def validate_personal_restore(name: str, image: str, schema: str = PERSONAL_SCHEMA) -> None:
    """Inspect frozen 0009/0010 contracts in synthetic PG, never migrate.

    Requires the approved running admin image to contain migration 0010. An old
    application image is NOT a compatible substitute. No production environment
    or mount enters the inspector, which shares only the network-none lab's
    loopback namespace. Both auxiliary resources are bounded and UUID-owned.
    """
    if (
        not re.fullmatch(r"time-tl-rc24-restore-[0-9a-f]{32}", name)
        or image != PG_IMAGE
        or schema not in RESTORE_INSPECTORS
    ):
        raise ValueError("Restore inspector target")
    config = monitor.protected_read(CONFIG)
    monitor.validate_config(config)
    records = json.loads(command(["docker", "inspect", name, ADMIN], limit=1024 * 1024))
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("Restore inspector identity")
    postgres, admin = records
    id_pattern = r"[0-9a-f]{64}"
    if (
        not re.fullmatch(id_pattern, postgres.get("Id", ""))
        or postgres.get("Name") != "/" + name
        or postgres.get("Image") != image
        or postgres.get("HostConfig", {}).get("NetworkMode") != "none"
        or postgres.get("State", {}).get("Running") is not True
        or admin.get("Name") != "/" + ADMIN
        or admin.get("Config", {}).get("Image") != monitor.expected_admin_image(config)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", admin.get("Image", ""))
        or admin.get("State", {}).get("Running") is not True
    ):
        raise ValueError("Restore inspector identity")
    inspector_name = "time-tl-cal29-inspector-" + uuid.uuid4().hex
    label_key = "time-tl.backup-inspector"
    inspector, marker = RESTORE_INSPECTORS[schema]
    args = [
        "docker",
        "create",
        "--name",
        inspector_name,
        "--label",
        "time-tl.backup-inspector=" + inspector_name,
        "--network",
        "container:" + postgres["Id"],
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--log-driver",
        "none",
        "--user",
        "10001:10001",
        "--entrypoint",
        "python",
        admin["Image"],
        "-c",
        inspector,
    ]
    owned_id = None
    try:
        created = command(args, timeout=30, limit=65536).decode().strip()
        if not re.fullmatch(id_pattern, created):
            raise ValueError("Restore inspector create")
        records = json.loads(command(["docker", "inspect", created], limit=1024 * 1024))
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("Restore inspector container")
        actual = records[0]
        if (
            actual.get("Id") != created
            or actual.get("Name") != "/" + inspector_name
            or actual.get("Image") != admin["Image"]
            or actual.get("Config", {}).get("Labels", {}).get(label_key) != inspector_name
            or actual.get("State", {}).get("Running") is not False
            or actual.get("HostConfig", {}).get("NetworkMode") != "container:" + postgres["Id"]
        ):
            raise ValueError("Restore inspector container")
        owned_id = created
        result = command(["docker", "start", "-a", owned_id], timeout=60, limit=65536)
        if result != marker:
            raise ValueError("Restore inspector validation")
        terminal = json.loads(
            command(
                ["docker", "inspect", owned_id],
                timeout=15,
                limit=1024 * 1024,
            )
        )[0]
        state = terminal.get("State", {})
        if (
            terminal.get("Id") != owned_id
            or state.get("Running") is not False
            or type(state.get("ExitCode")) is not int
            or state["ExitCode"] != 0
        ):
            raise ValueError("Restore inspector terminal state")
    finally:
        if owned_id is None:
            # CREATE can time out after Docker creates a stopped container. Only
            # adopt it for removal after full UUID-name/label/image validation.
            # Original CREATE failure is never turned into cleanup/restore PASS.
            inspector_creators_quiescent(inspector_name)
            records = json.loads(
                command(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--no-trunc",
                        "--format",
                        "{{json .}}",
                        "--filter",
                        "label=time-tl.backup-inspector=" + inspector_name,
                    ],
                    timeout=15,
                    limit=65536,
                )
                .decode()
                .strip()
                or "null"
            )
            if records is not None:
                candidate_id = records.get("ID", "")
                if not re.fullmatch(id_pattern, candidate_id):
                    raise ValueError("Restore inspector cleanup identity")
                actual = json.loads(
                    command(
                        ["docker", "inspect", candidate_id],
                        timeout=15,
                        limit=1024 * 1024,
                    )
                )[0]
                if (
                    actual.get("Id") != candidate_id
                    or actual.get("Name") != "/" + inspector_name
                    or actual.get("Image") != admin["Image"]
                    or actual.get("Config", {}).get("Labels", {}).get(label_key) != inspector_name
                    or actual.get("State", {}).get("Running") is not False
                ):
                    raise ValueError("Restore inspector cleanup identity")
                owned_id = candidate_id
        if owned_id is not None:
            actual = json.loads(
                command(
                    ["docker", "inspect", owned_id],
                    timeout=15,
                    limit=1024 * 1024,
                )
            )[0]
            if (
                actual.get("Id") != owned_id
                or actual.get("Name") != "/" + inspector_name
                or actual.get("Image") != admin["Image"]
                or actual.get("Config", {}).get("Labels", {}).get(label_key) != inspector_name
            ):
                raise ValueError("Restore inspector cleanup identity")
            command(["docker", "rm", "-f", owned_id], timeout=30, limit=65536)
            inspector_creators_quiescent(inspector_name)
            for selector in (
                "id=" + owned_id,
                "name=^/" + inspector_name + "$",
                "label=time-tl.backup-inspector=" + inspector_name,
            ):
                remaining = command(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--no-trunc",
                        "--format",
                        "{{.ID}}",
                        "--filter",
                        selector,
                    ],
                    timeout=15,
                    limit=65536,
                )
                if remaining.strip():
                    raise ValueError("Restore inspector cleanup unconfirmed")


def restore(dump: bytes, image: str, *, expected_schema: str | None = None) -> dict[str, Any]:
    if not dump.startswith(b"PGDMP") or len(dump) > LIMIT:
        raise ValueError("Dump format")
    if expected_schema is not None and expected_schema not in SCHEMA_TABLES:
        raise ValueError("Unsupported expected schema")
    name = "time-tl-rc24-restore-" + uuid.uuid4().hex
    try:
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--read-only",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "none",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=268435456",
                "--tmpfs",
                "/var/run/postgresql:rw,size=8388608",
                "--tmpfs",
                "/tmp:rw,size=8388608",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_USER=backupcheck",
                "-e",
                "POSTGRES_DB=backupcheck",
                image,
            ],
            timeout=30,
        )
        for _ in range(30):
            try:
                command(["docker", "exec", name, "pg_isready", "-U", "backupcheck"], timeout=5)
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Restore startup")
        toc = command(["docker", "exec", "-i", name, "pg_restore", "--list"], dump).decode()
        tables = sorted(
            re.findall(
                r"^\d+; \d+ \d+ TABLE public ([A-Za-z_][A-Za-z_0-9]*) \S+$", toc, re.MULTILINE
            )
        )
        if len(set(tables)) != len(tables) or frozenset(tables) not in SCHEMA_TABLES.values():
            raise ValueError("Dump table catalogue")
        command(
            [
                "docker",
                "exec",
                "-i",
                name,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-U",
                "backupcheck",
                "-d",
                "backupcheck",
            ],
            dump,
        )

        def query(sql: str) -> str:
            return (
                command(
                    [
                        "docker",
                        "exec",
                        name,
                        "psql",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        "backupcheck",
                        "-d",
                        "backupcheck",
                        "-Atc",
                        sql,
                    ]
                )
                .decode()
                .strip()
            )

        restored = query(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        schema = query("SELECT version_num FROM alembic_version")
        if (
            restored != tables
            or schema not in SCHEMA_TABLES
            or frozenset(tables) != SCHEMA_TABLES.get(schema)
            or (expected_schema is not None and schema != expected_schema)
        ):
            raise ValueError("Restored schema")
        if (
            query(
                "SELECT count(*) FROM pg_constraint "
                "WHERE connamespace='public'::regnamespace AND NOT convalidated"
            )
            != "0"
        ):
            raise ValueError("Unvalidated constraints")
        if schema in RESTORE_INSPECTORS:
            validate_personal_restore(name, image, schema)
        counts = {table: int(query('SELECT count(*) FROM "' + table + '"')) for table in tables}
        return {
            "schema": schema,
            "tables_checked": len(counts),
            "constraint_validation": "PASS",
            "restored_total_rows": sum(counts.values()),
            "row_counts_sha256": hashlib.sha256(
                json.dumps(counts, sort_keys=True).encode()
            ).hexdigest(),
            "postgres_image_id": image,
            "restore": "PASS",
            "persistent_plaintext_file_created": False,
            "restore_storage": "isolated network-none PostgreSQL tmpfs",
        }
    finally:
        # Only this UUID-owned isolated container; never production or retained volumes.
        command(["docker", "rm", "-f", name], timeout=30, limit=65536)


def publish_timestamp(verified_at: str) -> None:
    import fcntl

    secure_directory(CONFIG.parent)
    lock = CONFIG.parent / "state" / "collector.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Lock ownership")
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Collector lock deadline") from None
                time.sleep(0.1)
        config = monitor.protected_read(CONFIG)
        monitor.validate_config(config)
        previous = json.loads(json.dumps(config))
        config["backup_verified_at"] = verified_at
        try:
            durable_json(CONFIG, config)
        except Exception:
            # A failed readback/fsync may follow replacement: restore prior guard
            # under the same collector lock, never let REJECTED mean a new age.
            try:
                monitor.atomic_write(CONFIG, previous)
                parent_fd = os.open(CONFIG.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                if monitor.protected_read(CONFIG) != previous:
                    raise ValueError("Guard rollback readback")
            except Exception:
                raise GuardPublicationAmbiguous("Guard rollback ambiguous") from None
            raise
    finally:
        os.close(fd)


def durable_json(path: Path, value: Any) -> None:
    monitor.atomic_write(path, value)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if monitor.protected_read(path) != value:
        raise ValueError("Metadata readback")


def backup() -> dict[str, Any]:
    secure_directory(DIRECTORY)
    if shutil.disk_usage(DIRECTORY).free < 2 * LIMIT + 256 * 1024 * 1024:
        raise ValueError("Backup space")
    image = identity()
    started = datetime.now(UTC).isoformat()
    dump = command(
        [
            "docker",
            "exec",
            PG,
            "pg_dump",
            "-U",
            "time_tl",
            "-d",
            "time_tl",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--lock-wait-timeout=10s",
        ],
        limit=LIMIT,
    )
    encrypted = crypt(dump, "encrypt")
    del dump
    name = (
        "backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".tlbk"
    )
    path = DIRECTORY / name
    write_artifact(path, encrypted)
    persisted = read_artifact(path)
    if persisted != encrypted:
        raise ValueError("Persisted readback")
    result = restore(crypt(persisted, "decrypt"), image)
    current_schema = (
        command(
            [
                "docker",
                "exec",
                PG,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "time_tl",
                "-d",
                "time_tl",
                "-Atc",
                "SELECT version_num FROM alembic_version",
            ],
            limit=256,
        )
        .decode()
        .strip()
    )
    if current_schema != result["schema"]:
        raise ValueError("Schema changed during backup; guard not advanced")
    verified = datetime.now(UTC).isoformat()
    receipt = dict(
        result,
        status="PASS",
        started_at=started,
        verified_at=verified,
        artifact=name,
        bytes=len(persisted),
        sha256=hashlib.sha256(persisted).hexdigest(),
        snapshot="pg_dump consistent MVCC; no comparison with changing live rows",
        encryption="HKDF-SHA256/AES-256-GCM purpose separated existing OWNER_ALERTS_FERNET_KEY",
        retention_deletion_before_verification=False,
    )
    durable_json(DIRECTORY / (name + ".receipt.json"), receipt)
    # Timestamp represents backup SNAPSHOT age, not restore completion time.
    publish_timestamp(started)
    # Retention executes only after a newly saved backup has actually restored.
    # Failure cannot invalidate that successful copy or disable the freshness guard.
    from scripts.deployment import backup_control_host

    try:
        receipt["retention"] = backup_control_host.retention(datetime.now(UTC))
    except Exception:
        receipt["retention"] = {"status": "REJECTED", "classification": "retention_failed"}
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "verify-artifact"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--event-log", action="store_true")
    args = parser.parse_args()
    event_fd = None

    def emit(value: dict[str, Any]) -> None:
        raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
        if event_fd is None:
            print(raw.decode(), end="")
        else:
            offset = 0
            while offset < len(raw):
                written = os.write(event_fd, raw[offset:])
                if written <= 0:
                    raise OSError("Event write failed")
                offset += written
            os.fsync(event_fd)

    try:
        if args.event_log:
            state = CONFIG.parent / "state"
            secure_directory(state)
            event_fd = os.open(
                state / "backup-events.jsonl",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            info = os.fstat(event_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Event log protection")
        if args.mode == "backup":
            result = backup()
        else:
            if args.path is None:
                raise ValueError("Artifact required")
            raw = read_artifact(args.path)
            result = restore(crypt(raw, "decrypt"), identity())
            result.update(
                status="PASS", sha256=hashlib.sha256(raw).hexdigest(), timestamp_updated=False
            )
        emit(result)
        return 0
    except Exception as exc:
        rejection = {
            "status": "REJECTED",
            "classification": (
                "operator_verification_required"
                if isinstance(exc, GuardPublicationAmbiguous)
                else "backup_verification_failed"
            ),
        }
        try:
            # Never append to a descriptor whose protection check failed.
            if event_fd is not None:
                info = os.fstat(event_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    os.close(event_fd)
                    event_fd = None
            emit(rejection)
        except Exception:
            print(json.dumps(rejection))
        return 2
    finally:
        if event_fd is not None:
            os.close(event_fd)


if __name__ == "__main__":
    raise SystemExit(main())
