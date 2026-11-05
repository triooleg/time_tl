import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/deployment/package_rc29.py"
SPEC = importlib.util.spec_from_file_location("rc29_packet", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def sources(kind):
    files = {
        module.SITE + "runtime/contracts.py": b"contract",
        "/app/" + module.MIGRATION: b"migration",
    }
    if kind == "candidate":
        files.update({module.SITE + name: b"public code" for name in module.MODULES})
    return files


@pytest.mark.parametrize("kind", ["candidate", "fallback"])
def test_reproducible_exact_public_packet_and_no_actions(kind):
    files = sources(kind)
    raw = module.packet("a" * 40, "b" * 40, kind, files)
    assert raw == module.packet("a" * 40, "b" * 40, kind, files)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        assert len(tar.getmembers()) == len(files) + 2
        for item in tar.getmembers():
            assert item.isfile() and item.mode == 0o444 and item.mtime == 0
            assert not item.name.startswith("/") and ".." not in item.name.split("/")
            assert not item.linkname and item.uid == item.gid == 0
        provenance = json.load(tar.extractfile("provenance.json"))
        assert provenance["migration_head"] == module.HEAD
        assert provenance["personal_changes_enabled"] is (kind == "candidate")
        assert provenance["base_image"] == module.BASE_IMAGE
        dockerfile = tar.extractfile("Dockerfile").read()
        assert b"RUN " not in dockerfile and b"ENV " not in dockerfile
        assert b"/app/migrations/versions/0010_personal_calendar_overrides.py" in dockerfile


def test_extra_missing_secret_path_or_unknown_kind_rejected():
    files = sources("fallback")
    with pytest.raises(ValueError):
        module.packet("a" * 40, "b" * 40, "unknown", files)
    with pytest.raises(ValueError):
        module.packet("a" * 40, "b" * 40, "fallback", {})
    files["/home/example-operator/.env"] = b"never include"
    with pytest.raises(ValueError):
        module.packet("a" * 40, "b" * 40, "fallback", files)


def test_commit_with_omitted_source_fails_before_any_packet_write(monkeypatch, tmp_path):
    def git(repo, *args):
        if args[:2] == ("rev-parse", "HEAD"):
            return b"a" * 40
        if args[:2] == ("rev-parse", "HEAD^{}"):
            return b"a" * 40
        if args[:2] == ("rev-parse", "HEAD^"):
            return module.BASE_COMMIT.encode()
        if args[:2] == ("rev-parse", "HEAD^{tree}"):
            return b"b" * 40
        if args[0] == "diff":
            return b"src/time_tl/alerts/unreviewed.py\n"
        raise AssertionError(args)

    monkeypatch.setattr(module, "git", git)
    output = tmp_path / "packets"
    with pytest.raises(ValueError, match="absent from packet"):
        module.prepare(tmp_path, output)
    assert not output.exists()


def test_repair_source_is_one_exact_reviewed_substitution():
    before = b"def _uuid(value):\n    _require(type(value) is UUID)\n"
    after = (
        b"def _uuid(value):\n"
        b"    # asyncpg returns a UUID subclass for real PostgreSQL rows.\n"
        b"    _require(isinstance(value, UUID))\n"
    )
    module.validate_uuid_repair(before, after)
    module.validate_uuid_repair(before.replace(b"\n", b"\r\n"), after)
    for invalid in (before, after+b"unreviewed\n", after.replace(b"isinstance", b"permissive")):
        with pytest.raises(ValueError):
            module.validate_uuid_repair(before, invalid)


@pytest.mark.parametrize("kind", ["candidate", "fallback"])
def test_repair_packet_distinct_provenance_and_reproducible(kind):
    files = sources(kind)
    raw = module.packet("a"*40, "b"*40, kind, files, uuid_repair=True)
    assert raw == module.packet("a"*40, "b"*40, kind, files, uuid_repair=True)
    assert raw != module.packet("a"*40, "b"*40, kind, files)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        value = json.load(tar.extractfile("provenance.json"))
        assert value["uuid_repair_parent"] == module.UUID_REPAIR_PARENT
        assert value["base_commit"] == module.BASE_COMMIT


@pytest.mark.parametrize("changed", [
    b"", b"src/time_tl/config.py\n",
    b"src/time_tl/domain/calendar_overrides.py\nsrc/time_tl/config.py\n",
])
def test_repair_extra_application_changes_rejected_before_io(monkeypatch, tmp_path, changed):
    def git(repo, *args):
        if args == ("rev-parse", "HEAD^"):
            return module.UUID_REPAIR_PARENT.encode()
        if args[0] == "rev-parse":
            return b"a"*40
        if args[0] == "diff":
            return changed
        raise AssertionError(args)
    monkeypatch.setattr(module, "git", git)
    output = tmp_path/"uncreated"
    with pytest.raises(ValueError, match="repair application paths"):
        module.prepare(tmp_path, output, uuid_repair=True)
    assert not output.exists()
