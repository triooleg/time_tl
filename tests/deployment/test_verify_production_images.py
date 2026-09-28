from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deployment" / "verify_production_images.py"


def load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_production_images", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = load_verifier()
APP_IMAGE = "registry.example/time-tl@sha256:" + "a" * 64
POSTGRES_IMAGE = "postgres@sha256:" + "b" * 64
EXPORTER_IMAGE = "quay.io/prometheus/node-exporter@sha256:" + "c" * 64


def valid_environment() -> dict[str, str]:
    return {
        "TIME_TL_PRODUCTION_IMAGE": APP_IMAGE,
        "TIME_TL_POSTGRES_IMAGE": POSTGRES_IMAGE,
        "TIME_TL_NODE_EXPORTER_IMAGE": EXPORTER_IMAGE,
    }


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "registry.example/time-tl:latest",
        "registry.example/time-tl:release",
        "registry.example/time-tl@sha256:" + "a" * 63,
        "registry.example/time-tl@sha256:" + "A" * 64,
        "Registry.example/time-tl@sha256:" + "a" * 64,
        "registry.example/time-tl:tag@sha256:" + "a" * 64,
    ],
)
def test_image_verifier_rejects_non_exact_references(invalid: str) -> None:
    environment = valid_environment()
    environment["TIME_TL_PRODUCTION_IMAGE"] = invalid
    with pytest.raises(VERIFIER.VerificationError, match="TIME_TL_PRODUCTION_IMAGE"):
        VERIFIER.verify_environment_images(environment)


def test_image_verifier_accepts_three_exact_lowercase_digests() -> None:
    assert VERIFIER.verify_environment_images(valid_environment()) == valid_environment()


def test_environment_parser_rejects_duplicate_without_exposing_value(tmp_path: Path) -> None:
    environment = tmp_path / "production.env"
    environment.write_text(
        f"TIME_TL_PRODUCTION_IMAGE={APP_IMAGE}\nTIME_TL_PRODUCTION_IMAGE=secret\n",
        encoding="utf-8",
    )
    with pytest.raises(VERIFIER.VerificationError, match="duplicate TIME_TL_PRODUCTION_IMAGE"):
        VERIFIER.read_environment(environment)


def test_resolved_compose_json_must_match_every_expected_service(tmp_path: Path) -> None:
    environment = valid_environment()
    services = {}
    for environment_name, names in VERIFIER.IMAGE_ENVIRONMENTS.items():
        services.update({name: {"image": environment[environment_name]} for name in names})
    rendered = tmp_path / "compose.json"
    rendered.write_text(json.dumps({"services": services}), encoding="utf-8")
    VERIFIER.verify_compose_config(rendered, environment)

    services["bot"]["image"] = "registry.example/time-tl:latest"
    rendered.write_text(json.dumps({"services": services}), encoding="utf-8")
    with pytest.raises(VERIFIER.VerificationError, match="image mismatch for service bot"):
        VERIFIER.verify_compose_config(rendered, environment)
