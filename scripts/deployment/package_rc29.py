"""Reproducible committed RC29 candidate and head10-compatible fallback packets.

Only creates LOCAL public artifacts in an explicitly supplied new directory.
No build, upload, signing, migration or production authority is granted here.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

BASE_COMMIT = "24161c8f16d5a88853cf01ec4a20e512bc9bb73f"
UUID_REPAIR_PARENT = "e3b24751c635be0e8a128418637d1c7a955fac8c"
BASE_IMAGE = (
    "localhost:5000/time-tl-bot@sha256:"
    "72a6abb9c035fc4fb1d639b73b140f84917dacf05242b4a9f428a71489988b17"
)
APP = "src/time_tl/"
SITE = "/opt/venv/lib/python3.12/site-packages/time_tl/"
MODULES = (
    "bot/async_service.py",
    "bot/calendar_router.py",
    "bot/personal_calendar_router.py",
    "config.py",
    "domain/calendar_overrides.py",
    "infrastructure/db/calendar_overrides.py",
    "infrastructure/db/calendars.py",
    "infrastructure/db/models.py",
    "runtime/contracts.py",
    "runtime/services.py",
)
MIGRATION = "migrations/versions/0010_personal_calendar_overrides.py"
HEAD = "0010_personal_calendar_overrides"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError("Committed package read failed; output suppressed")
    return result.stdout


def validate_uuid_repair(before: bytes, after: bytes) -> None:
    before, after = before.replace(b"\r\n", b"\n"), after.replace(b"\r\n", b"\n")
    needle = b"    _require(type(value) is UUID)\n"
    replacement = (
        b"    # asyncpg returns a UUID subclass for real PostgreSQL rows.\n"
        b"    _require(isinstance(value, UUID))\n"
    )
    if before.count(needle) != 1 or before.replace(needle, replacement) != after:
        raise ValueError("UUID repair contains additional application changes")


def packet(commit: str, tree: str, kind: str, files: dict[str, bytes],
           *, uuid_repair: bool = False) -> bytes:
    if kind not in {"candidate", "fallback"}:
        raise ValueError("Invalid packet kind")
    expected = (
        {SITE + path for path in MODULES} | {"/app/" + MIGRATION}
        if kind == "candidate"
        else {SITE + "runtime/contracts.py", "/app/" + MIGRATION}
    )
    if set(files) != expected or any(
        type(raw) is not bytes or len(raw) > 524288 for raw in files.values()
    ):
        raise ValueError("Unexpected public packet source")
    source_map = {path: sha(raw) for path, raw in sorted(files.items())}
    if type(uuid_repair) is not bool:
        raise ValueError("Invalid UUID repair mode")
    provenance_value = {
            "scope": "RC29_PUBLIC_LOCAL_PACKET_NOT_DEPLOY_APPROVAL",
            "kind": kind,
            "commit": commit,
            "tree": tree,
            "base_commit": BASE_COMMIT,
            "base_image": BASE_IMAGE,
            "migration_head": HEAD,
            "personal_changes_enabled": kind == "candidate",
            "sources": source_map,
        }
    if uuid_repair:
        provenance_value["uuid_repair_parent"] = UUID_REPAIR_PARENT
    provenance = json.dumps(
        provenance_value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    dockerfile = (
        f"FROM {BASE_IMAGE}\n"
        + "".join(f"COPY --chmod=0444 public/{path.lstrip('/')} {path}\n" for path in sorted(files))
        + f'LABEL org.opencontainers.image.revision="{commit}" '
        + f'time-tl.source_tree="{tree}" time-tl.rc29.kind="{kind}" '
        + f'time-tl.rc29.provenance_sha256="{sha(provenance)}"\n'
    ).encode()
    members = {
        "Dockerfile": dockerfile,
        "provenance.json": provenance,
        **{"public/" + path.lstrip("/"): raw for path, raw in files.items()},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, raw in sorted(members.items()):
            entry = tarfile.TarInfo(name)
            entry.mode, entry.mtime, entry.size = 0o444, 0, len(raw)
            tar.addfile(entry, io.BytesIO(raw))
    return buffer.getvalue()


def prepare(repo: Path, output: Path, *, uuid_repair: bool = False) -> dict[str, object]:
    if type(uuid_repair) is not bool:
        raise ValueError("Invalid UUID repair mode")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if git(repo, "rev-parse", "HEAD^{}").decode().strip() == BASE_COMMIT:
        raise ValueError("RC29 requires a new explicitly authorized release commit")
    expected_parent = UUID_REPAIR_PARENT if uuid_repair else BASE_COMMIT
    if git(repo, "rev-parse", "HEAD^").decode().strip() != expected_parent:
        raise ValueError("Unexpected release parent; review new baseline")
    if uuid_repair:
        repair_path = APP + "domain/calendar_overrides.py"
        repair_changed = set(git(
            repo, "diff", "--name-only", UUID_REPAIR_PARENT, commit, "--", "src", "migrations"
        ).decode().splitlines())
        if repair_changed != {repair_path}:
            raise ValueError("Unexpected UUID repair application paths")
        validate_uuid_repair(
            git(repo, "show", f"{UUID_REPAIR_PARENT}:{repair_path}"),
            git(repo, "show", f"{commit}:{repair_path}"),
        )
    tree = git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    allowed = {APP + name for name in MODULES} | {MIGRATION}
    changed = set(
        git(repo, "diff", "--name-only", BASE_COMMIT, commit, "--", "src", "migrations")
        .decode()
        .splitlines()
    )
    if not changed <= allowed or not changed:
        raise ValueError("Commit contains application changes absent from packet")
    if any(path.is_symlink() for path in (repo, *repo.parents, output, *output.parents)):
        raise ValueError("Packet path contains symlink")
    candidate = {}
    for module in MODULES:
        source = APP + module
        raw = git(repo, "show", f"{commit}:{source}")
        if (repo / source).read_bytes().replace(b"\r\n", b"\n") != raw.replace(b"\r\n", b"\n"):
            raise ValueError("Uncommitted candidate source drift")
        candidate[SITE + module] = raw
    migration = git(repo, "show", f"{commit}:{MIGRATION}")
    if (repo / MIGRATION).read_bytes().replace(b"\r\n", b"\n") != migration.replace(b"\r\n", b"\n"):
        raise ValueError("Uncommitted migration drift")
    candidate["/app/" + MIGRATION] = migration
    contract = git(repo, "show", f"{BASE_COMMIT}:{APP}runtime/contracts.py")
    old = b'"migration_head": "0009_calendar_foundation"'
    if contract.count(old) != 1:
        raise ValueError("Unexpected frozen fallback contract")
    fallback = {
        SITE + "runtime/contracts.py": contract.replace(
            old, f'"migration_head": "{HEAD}"'.encode()
        ),
        "/app/" + MIGRATION: migration,
    }
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    receipts: dict[str, object] = {
        "scope": "RC29_LOCAL_COMMITTED_PACKETS_ONLY",
        "commit": commit,
        "tree": tree,
        "base_commit": BASE_COMMIT,
        "base_image": BASE_IMAGE,
        "migration_head": HEAD,
        "config_revision": 15,
        "packets": {},
        "build": False,
        "deployed": False,
        "signed": False,
    }
    if uuid_repair:
        receipts["uuid_repair_parent"] = UUID_REPAIR_PARENT
    for kind, files in (("candidate", candidate), ("fallback", fallback)):
        archive = packet(commit, tree, kind, files, uuid_repair=uuid_repair)
        label = "uuid-repair" if uuid_repair else "personal-calendar"
        name = f"time-tl-production-20261004-rc29-{label}-{kind}.tar"
        with (output / name).open("xb") as stream:
            stream.write(archive)
        receipts["packets"][kind] = {"archive": name, "sha256": sha(archive), "bytes": len(archive)}
    with (output / "PACKETS.json").open("x", encoding="utf-8") as stream:
        json.dump(receipts, stream, indent=2)
    return receipts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--uuid-repair", action="store_true")
    options = parser.parse_args()
    print(json.dumps(prepare(options.repo.resolve(), options.output.absolute(),
                             uuid_repair=options.uuid_repair)))
