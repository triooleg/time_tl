"""Colocated, fail-closed operational monitor; not independent release evidence.

Run collector and watchdog under separate flock locks. Configuration and state
contain public aggregate metadata only. Never restarts application containers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROLES = ("sender-worker", "schedule-worker", "bot", "alert-worker")
ALL_ROLES = (*ROLES, "admin-web")
MODE = "COLOCATED"
STACK = "time-tl-production"
TRUSTED_STATE = Path("/home/trio/time-tl-monitor-rc24/state")
# Independently checked admin-only overlay; RC24 bot/worker identity is unchanged.
RC25_ADMIN_IMAGE: str | None = (
    "localhost:5000/time-tl-bot@sha256:"
    "2edc2a0347b565ba49834d87a289b62f0b9f83c559c61db49af67301df55c9b0"
)
SQL = """SELECT json_build_object(
 'state_active_revision', (SELECT (value->>'active_revision')::int FROM runtime_settings
 WHERE key='owner_configuration_state'),
 'active_revision', (SELECT (value->>'revision')::int FROM runtime_settings
 WHERE key='owner_configuration_active'),
 'provider_freshness_seconds', (SELECT EXTRACT(EPOCH FROM now()-last_success_at)
 FROM alert_poll_state WHERE singleton_id=1),
 'provider_circuit_healthy', (SELECT NOT circuit_open AND consecutive_failures=0
 FROM alert_poll_state WHERE singleton_id=1),
 'heartbeats', (SELECT json_object_agg(worker_name,json_build_object(
 'age_seconds',EXTRACT(EPOCH FROM now()-heartbeat_at),'status',status,
 'applied_revision',details->'applied_config_revision')) FROM worker_heartbeats
 WHERE worker_name IN ('bot','alert-worker','schedule-worker','sender-worker')),
 'outbox_open_count',count(*) FILTER(WHERE delivered_at IS NULL AND dead_at IS NULL),
 'outbox_oldest_open_age_seconds',COALESCE(max(EXTRACT(EPOCH FROM now()-created_at))
 FILTER(WHERE delivered_at IS NULL AND dead_at IS NULL),0),
 'outbox_dead_total',count(*) FILTER(WHERE dead_at IS NOT NULL),
 'outbox_retry_total',COALESCE(sum(attempts),0),'database_healthy',true
 ) FROM outbox_messages"""


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def protected_read(path: Path, limit: int = 65536) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("State ownership or type")
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit:
            raise ValueError("State permissions or size")
        with os.fdopen(fd, "r", closefd=False) as stream:
            return json.loads(stream.read(limit + 1), object_pairs_hook=unique)
    finally:
        os.close(fd)


def atomic_write(path: Path, value: Any) -> None:
    raw = json.dumps(value, allow_nan=False, sort_keys=True).encode()
    fd, name = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(args: list[str], *, payload: str | None = None, timeout: int = 15) -> str:
    result = subprocess.run(
        args, input=payload, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError("Subprocess failed")
    return result.stdout


def expected_admin_image(config: dict[str, Any]) -> str:
    if "admin_image" not in config:
        return config["image"]
    image = config["admin_image"]
    if (
        RC25_ADMIN_IMAGE is None
        or image != RC25_ADMIN_IMAGE
        or not isinstance(image, str)
        or re.fullmatch(r"localhost:5000/time-tl-bot@sha256:[a-f0-9]{64}", image) is None
    ):
        raise ValueError("Admin image identity")
    return image


def expected_role_images(config: dict[str, Any]) -> dict[str, str]:
    return {**dict.fromkeys(ROLES, config["image"]), "admin-web": expected_admin_image(config)}


def validate_config(config: Any) -> None:
    expected = {
        "state_dir",
        "image",
        "revision",
        "baseline",
        "threshold_environment",
        "backup_verified_at",
        "tls_hostname",
        "lib_dir",
    }
    if not isinstance(config, dict) or set(config) not in (expected, expected | {"admin_image"}):
        raise ValueError("Public configuration schema")
    if type(config["revision"]) is not int or config["revision"] != 14:
        raise ValueError("Revision identity")
    if config["image"] != (
        "localhost:5000/time-tl-bot@sha256:"
        "060e2ec4ce1e72cfe4984b6b0e790b14f93ccf82b1498958dd5d334a7c2ae391"
    ):
        raise ValueError("Image identity")
    state = Path(config["state_dir"])
    if state != TRUSTED_STATE:
        raise ValueError("Unexpected state path")
    if config["lib_dir"] != "/home/trio/time-tl-monitor-rc24/lib":
        raise ValueError("Unexpected public library path")
    info = state.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("State directory permissions")
    if set(config["baseline"]["container_ids"]) != set(ALL_ROLES):
        raise ValueError("Baseline identity incomplete")
    expected_admin_image(config)
    if "admin_image" in config:
        baseline = config["baseline"]
        admin_id = baseline["container_ids"]["admin-web"]
        restarts = baseline.get("restarts", {})
        if (
            not isinstance(admin_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", admin_id) is None
            or set(restarts) != set(ALL_ROLES)
            or any(type(count) is not int or count < 0 for count in restarts.values())
        ):
            raise ValueError("Admin handoff baseline incomplete")


def collect(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    sample = json.loads(
        run(
            [
                "docker",
                "exec",
                STACK + "-postgres-1",
                "psql",
                "-U",
                "time_tl",
                "-d",
                "time_tl",
                "-Atc",
                SQL,
            ]
        )
    )
    sample.update(restarts={}, container_ids={}, images={})
    inspections = json.loads(
        run(["docker", "inspect", *[STACK + "-" + r + "-1" for r in ALL_ROLES]])
    )
    for role, item in zip(ALL_ROLES, inspections, strict=True):
        if (
            not item["State"]["Running"]
            or item["State"].get("Health", {}).get("Status") != "healthy"
        ):
            raise ValueError("Application role unhealthy")
        if not any(
            m.get("Destination") == "/run/time-tl-release" and m.get("RW") is False
            for m in item.get("Mounts", [])
        ):
            raise ValueError("Read-only attestation missing")
        sample["restarts"][role] = item["RestartCount"]
        sample["container_ids"][role] = item["Id"]
        sample["images"][role] = item["Config"]["Image"]
    if (
        sample["active_revision"] != config["revision"]
        or sample["state_active_revision"] != config["revision"]
    ):
        raise ValueError("Active revision drift")
    if any(
        sample["heartbeats"].get(r, {}).get("applied_revision") != config["revision"] for r in ROLES
    ):
        raise ValueError("Applied revision drift")
    disk = shutil.disk_usage("/")
    memory = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemTotal:", "MemAvailable:"))
    }
    host = config["tls_hostname"]
    # Colocated probe avoids public-DNS hairpin routing. Certificate validation and
    # SNI retain the public hostname; this does NOT test external reachability.
    with socket.create_connection(("127.0.0.1", 443), timeout=10) as conn:
        with ssl.create_default_context().wrap_socket(conn, server_hostname=host) as tls:
            expiry = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    backup = datetime.fromisoformat(config["backup_verified_at"])
    if backup.tzinfo is None:
        raise ValueError("Backup timestamp timezone")
    sample.update(
        observed_at=now.isoformat(),
        tls_remaining_days=(expiry - now.timestamp()) / 86400,
        verified_backup_age_seconds=(now - backup).total_seconds(),
        disk_free_percent=100 * disk.free / disk.total,
        memory_available_percent=100 * memory["MemAvailable"] / memory["MemTotal"],
    )
    return sample


EVALUATOR = """import sys,json
from datetime import UTC,datetime
sys.path.insert(0,'/monitor-lib')
from scripts.deployment.offhost_monitor import observations
from scripts.deployment.verify_production_monitoring import (
 verify_monitoring_environment,verify_monitoring_evidence)
p=json.load(sys.stdin);s=p['sample'];b=p['baseline'];now=datetime.now(UTC)
age=(now-datetime.fromisoformat(s['observed_at'])).total_seconds()
if not 0<=age<=60: raise ValueError('Stale observation')
if s['container_ids']!=b['container_ids']: raise ValueError('Container replacement')
if s['images']!=p['role_images']: raise ValueError('Image drift')
o=observations(s,b)
for f in ('provider_freshness_seconds','max_worker_heartbeat_age_seconds',
          'verified_backup_age_seconds'):o[f]+=age
if o['outbox_open_count']:o['outbox_oldest_open_age_seconds']+=age
o['tls_remaining_days']-=age/86400
# Validation only: this synthetic envelope is NEVER persisted, signed or used at a release gate.
v={'schema_version':1,'generated_at':now.isoformat(),
 'notification_route_exercised_at':now.isoformat(),
 'notification_route_receipt_sha256':'0'*64,'observations':o,
 'stop_authority':'colocated operational HOLD','rollback_authority':'operator'}
verify_monitoring_evidence(v,verify_monitoring_environment(p['environment']),now=now)
print('PASS')
"""


def evaluate(config: dict[str, Any], sample: dict[str, Any]) -> None:
    packet = dict(
        sample=sample,
        baseline=config["baseline"],
        image=config["image"],
        role_images=expected_role_images(config),
        environment=config["threshold_environment"],
    )
    # Program and JSON supplied separately without exposing any application credentials.
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            "-v",
            config["lib_dir"] + ":/monitor-lib:ro",
            config["image"],
            "-c",
            EVALUATOR,
        ],
        payload=json.dumps(packet),
        timeout=25,
    )
    if result.strip() != "PASS":
        raise ValueError("Threshold evaluation unconfirmed")


def verify_pulse(value: Any, config: dict[str, Any], now: datetime) -> None:
    fields = {
        "schema_version",
        "mode",
        "stack",
        "revision",
        "image",
        "observed_at",
        "expires_at",
        "status",
    }
    if "admin_image" in config:
        fields.add("admin_image")
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Pulse schema")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["mode"] != MODE
        or value["stack"] != STACK
        or value["status"] != "PASS"
        or type(value["revision"]) is not int
        or value["revision"] != config["revision"]
        or value["image"] != config["image"]
    ):
        raise ValueError("Pulse identity")
    if "admin_image" in config and value["admin_image"] != expected_admin_image(config):
        raise ValueError("Pulse admin identity")
    observed, expiry = (datetime.fromisoformat(value[k]) for k in ("observed_at", "expires_at"))
    if (
        observed.tzinfo is None
        or expiry.tzinfo is None
        or not 0 < (expiry - observed).total_seconds() <= 120
    ):
        raise ValueError("Pulse duration")
    if not observed <= now < expiry:
        raise ValueError("Pulse expired or future")


def stop_roles() -> list[str]:
    stopped = []
    for role in ROLES:
        try:
            run(["docker", "stop", "--time", "10", STACK + "-" + role + "-1"], timeout=20)
            stopped.append(role)
        except Exception:
            pass
    return stopped


def notify(state: Path, *, route_test: bool = False) -> None:
    # One attempt per HOLD episode, even if delivery is ambiguous.
    marker = "route-test.claim" if route_test else "notify.claim"
    fd = os.open(state / marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    query = (
        "SELECT value->'staging_allowlist' FROM runtime_settings "
        "WHERE key='owner_configuration_active'"
    )
    ids = json.loads(
        run(
            [
                "docker",
                "exec",
                "time-tl-bot-postgres-1",
                "psql",
                "-U",
                "time_tl",
                "-d",
                "time_tl",
                "-Atc",
                query,
            ]
        )
    )
    if not isinstance(ids, list) or len(ids) != 1 or type(ids[0]) is not int:
        raise ValueError("Operator allowlist invalid")
    program = """import asyncio
from aiogram import Bot
from time_tl.config import Settings
from time_tl.runtime.services import _postgres_admin_repository
from time_tl.admin.settings import OwnerSettingsService
async def main():
 s=Settings();e,r=_postgres_admin_repository(s)
 try:
  svc=OwnerSettingsService(r,s.owner_telegram_fernet_key.get_secret_value(),s.owner_alerts_fernet_key.get_secret_value())
  c=await svc.active_runtime_config('production','bot');b=Bot(c.telegram_token.get_secret_value())
  try:
   await b.send_message(OPERATOR_ID,MESSAGE_TEXT)
  finally:await b.session.close()
 finally:await e.dispose()
asyncio.run(main())
""".replace("OPERATOR_ID", str(ids[0])).replace(
        "MESSAGE_TEXT",
        repr(
            (
                "Time TL: проверка технического канала локального мониторинга. "
                "Это не уведомление о тревоге."
            )
            if route_test
            else (
                "Time TL: локальный монитор минисервера HOLD. "
                "Выполнена попытка остановки отправки; требуется проверка оператора. "
                "Это техническое уведомление, не отбой тревоги. "
                "Монитор не защищает от полного отказа этого сервера."
            )
        ),
    )
    run(
        ["docker", "exec", "-i", STACK + "-admin-web-1", "python", "-"], payload=program, timeout=35
    )


def hold(state: Path) -> int:
    try:
        atomic_write(
            state / "hold.json",
            {"mode": MODE, "status": "HOLD", "at": datetime.now(UTC).isoformat()},
        )
    except Exception:
        pass
    stop_roles()
    try:
        notify(state)
    except Exception:
        pass
    return 2


def execute(config: dict[str, Any], mode: str) -> int:
    state = TRUSTED_STATE
    try:
        validate_config(config)
        state = Path(config["state_dir"])
        if mode == "route-test":
            notify(state, route_test=True)
            return 0
        if mode == "watchdog":
            if os.path.lexists(state / "hold.json"):
                raise ValueError("Persistent HOLD")
            verify_pulse(protected_read(state / "pulse.json", 4096), config, datetime.now(UTC))
            return 0
        if mode == "once" and os.path.lexists(state / "hold.json"):
            raise ValueError("Persistent HOLD")
        sample = collect(config)
        evaluate(config, sample)
        if mode in ("shadow", "route-test"):
            print(json.dumps({"mode": MODE, "status": "PASS", "sample": sample}, allow_nan=False))
            return 0
        now = datetime.now(UTC)
        atomic_write(
            state / "pulse.json",
            dict(
                schema_version=1,
                mode=MODE,
                stack=STACK,
                revision=config["revision"],
                image=config["image"],
                **(
                    {"admin_image": expected_admin_image(config)}
                    if "admin_image" in config else {}
                ),
                status="PASS",
                observed_at=sample["observed_at"],
                expires_at=(
                    datetime.fromisoformat(sample["observed_at"]) + timedelta(seconds=120)
                ).isoformat(),
            ),
        )
        atomic_write(state / "last-pass.json", {"mode": MODE, "at": now.isoformat()})
        if mode == "initialize":
            (state / "hold.json").unlink(missing_ok=True)
            (state / "notify.claim").unlink(missing_ok=True)
        return 0
    except Exception:
        if mode in ("shadow", "route-test"):
            print("COLOCATED SHADOW REJECTED")
            return 2
        return hold(state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("mode", choices=("shadow", "initialize", "once", "watchdog", "route-test"))
    args = parser.parse_args()
    try:
        config = protected_read(args.config)
    except Exception:
        if args.mode not in ("shadow", "route-test"):
            return hold(TRUSTED_STATE)
        return 2
    return execute(config, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
