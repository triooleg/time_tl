"""The public derivative remains available independently of fallback feature flags."""

import base64
import hashlib
import io
import json
import zipfile

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from time_tl.admin import weather_geography as module
from time_tl.runtime import weather_release


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def publication(tmp_path, monkeypatch):
    files = {name: name.encode() for name in module.PUBLIC_FILES}
    digest = sha(files["geography.csv"])
    metadata = encoded(
        {
            "files": {name: sha(data) for name, data in files.items()},
            "published_files": sorted(files),
            "normalized_sha256": digest,
            "license": "ODbL-1.0",
            "operational_catalog_redistributed": False,
        }
    )
    certificate = {
        "schema_version": 1,
        "kind": "time-tl-weather-geography-release",
        "profile": "primary",
        "mode": "public",
        "source_commit": "1" * 40,
        "archive_sha256": "2" * 64,
        "image_digest": "sha256:" + "3" * 64,
        "native_check_sha256": "4" * 64,
        "base_release_sha256": "5" * 64,
        "migration_head": weather_release.HEAD,
        "owner_revision": 15,
        "roles": sorted(weather_release.ROLE_CAPABILITIES),
        "capabilities": {cap: True for cap in weather_release.CAPABILITIES},
        "installed_files": {name: "6" * 64 for name in weather_release.REQUIRED_FILES},
        "geography_sha256": digest,
        "geography_manifest_sha256": sha(metadata),
        "source": "MET Norway",
    }
    raw = encoded(certificate)
    key = Ed25519PrivateKey.generate()  # Never serialized or written.
    monkeypatch.setattr(module.production_release, "_public_key", key.public_key)
    files["geography-manifest.json"] = metadata
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    (tmp_path / "weather-manifest.json").write_bytes(raw)
    (tmp_path / "weather-manifest.ed25519").write_bytes(base64.b64encode(key.sign(raw)))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    (tmp_path / "geography-bundle.zip").write_bytes(archive.getvalue())
    return tmp_path, digest


@pytest.mark.parametrize(
    "name", sorted(module.PUBLIC_FILES | {"geography-manifest.json", "geography-bundle.zip"})
)
def test_signed_public_artifacts_have_no_executing_image_requirement(publication, name):
    root, digest = publication
    assert module.public_artifact(digest, name, root=root) == (root / name).read_bytes()


@pytest.mark.parametrize("name", ["../active-catalog.json", "active-catalog.json", ".env"])
def test_private_or_arbitrary_files_are_not_published(publication, name):
    root, digest = publication
    with pytest.raises(ValueError):
        module.public_artifact(digest, name, root=root)


def test_tampered_public_bytes_and_signature_fail_closed(publication):
    root, digest = publication
    (root / "geography.csv").write_bytes(b"changed")
    with pytest.raises(ValueError):
        module.public_artifact(digest, "geography.csv", root=root)
    raw = (root / "weather-manifest.json").read_bytes()
    (root / "weather-manifest.json").write_bytes(raw.replace(b"MET Norway", b"other name"))
    with pytest.raises(InvalidSignature):
        module.public_artifact(digest, "geography-manifest.json", root=root)


def test_bundle_cannot_smuggle_catalogue_or_duplicate_member(publication):
    root, digest = publication
    with zipfile.ZipFile(root / "geography-bundle.zip", "a") as bundle:
        bundle.writestr("active-catalog.json", b"not public")
    with pytest.raises(ValueError):
        module.public_artifact(digest, "geography-bundle.zip", root=root)


def test_missing_certificate_is_public_404_without_account_or_details(monkeypatch):
    def missing(*args):
        raise weather_release._fail()

    monkeypatch.setattr(module, "public_artifact", missing)
    app = FastAPI()
    module.register_geography(app)
    with TestClient(app) as client:
        result = client.get("/weather/geography/" + "a" * 64 + "/geography.csv")
    assert result.status_code == 404 and result.content == b""
