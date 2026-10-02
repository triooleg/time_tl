from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
STAGING_COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.production.example"

APPLICATION_SERVICES = {
    "bot",
    "alert-worker",
    "schedule-worker",
    "sender-worker",
    "admin-web",
    "migrate",
    "production-preflight",
    "production-activate",
}
PUBLIC_ROLES = {"bot", "alert-worker", "schedule-worker", "sender-worker"}
STAGING_COMPOSE_SHA256 = "f97d646d27dcdcbd5a4e6096edae85d81a684f03bb91ccf99a05e5bb46bc3c93"
ATTESTATION_SOURCE = (
    "${TIME_TL_RELEASE_ATTESTATION_DIR:?set the operator-controlled attestation directory}"
)
SECRET_ENVIRONMENTS = {
    "OWNER_TELEGRAM_FERNET_KEY",
    "OWNER_ALERTS_FERNET_KEY",
    "LESSON_LINK_FERNET_KEY",
    "ADMIN_IP_HASH_KEY",
}


def load_compose() -> dict[str, Any]:
    value = yaml.safe_load(PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_base_staging_compose_is_byte_for_byte_unchanged() -> None:
    assert hashlib.sha256(STAGING_COMPOSE.read_bytes()).hexdigest() == STAGING_COMPOSE_SHA256
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "docker-compose.yml text eol=lf" in attributes
    assert "docker-compose.production.yml text eol=lf" in attributes
    assert "!.env.production.example" in (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()


def test_staging_homework_flag_is_bot_only_and_disabled_by_default() -> None:
    compose = yaml.safe_load(STAGING_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["bot"]["environment"]["HOMEWORK_ENABLED"] == (
        "${HOMEWORK_ENABLED:-false}"
    )
    assert all(
        "HOMEWORK_ENABLED" not in service.get("environment", {})
        for name, service in services.items()
        if name != "bot"
    )


def test_production_homework_is_literal_true_for_bot_only() -> None:
    services = load_compose()["services"]
    assert services["bot"]["environment"]["HOMEWORK_ENABLED"] == "true"
    assert all(
        "HOMEWORK_ENABLED" not in service.get("environment", {})
        for name, service in services.items()
        if name != "bot"
    )
    assert "${HOMEWORK_ENABLED" not in PRODUCTION_COMPOSE.read_text(encoding="utf-8")


def test_all_images_are_required_inputs_to_the_external_verifier() -> None:
    compose = load_compose()
    services = compose["services"]
    application_image = compose["x-production-app"]["image"]
    assert application_image.startswith("${TIME_TL_PRODUCTION_IMAGE:?")
    assert all(services[name]["image"] == application_image for name in APPLICATION_SERVICES)
    assert services["postgres"]["image"].startswith("${TIME_TL_POSTGRES_IMAGE:?")
    assert services["node-exporter"]["image"].startswith("${TIME_TL_NODE_EXPORTER_IMAGE:?")

    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    references = re.findall(r"^TIME_TL_[A-Z_]+IMAGE=([^\n]+)$", example, re.MULTILINE)
    assert len(references) == 3
    assert all(re.fullmatch(r"[^\s@]+@sha256:<64-lowercase-hex>", item) for item in references)


def test_resolved_role_environments_and_networks_are_least_privilege() -> None:
    compose = load_compose()
    services = compose["services"]
    common_environment = compose["x-production-app"]["environment"]
    assert common_environment["TIME_TL_ENV"] == "production"
    assert common_environment["OWNER_CONFIG_SOURCE"] == "database"
    assert common_environment["TEST_CHAT_ALLOWLIST"] == ""
    assert common_environment["ALLOW_SCAFFOLD_SERVICES"] == "false"
    assert not SECRET_ENVIRONMENTS.intersection(common_environment)

    expected_secrets = {
        "bot": {"OWNER_TELEGRAM_FERNET_KEY", "LESSON_LINK_FERNET_KEY"},
        "alert-worker": {"OWNER_ALERTS_FERNET_KEY"},
        "schedule-worker": {"LESSON_LINK_FERNET_KEY"},
        "sender-worker": {"OWNER_TELEGRAM_FERNET_KEY", "LESSON_LINK_FERNET_KEY"},
        "admin-web": {
            "ADMIN_IP_HASH_KEY",
            "OWNER_TELEGRAM_FERNET_KEY",
            "OWNER_ALERTS_FERNET_KEY",
        },
        "migrate": set(),
        "production-preflight": SECRET_ENVIRONMENTS,
        "production-activate": SECRET_ENVIRONMENTS,
    }
    expected_networks = {
        "bot": {"egress", "database"},
        "alert-worker": {"egress", "database"},
        "schedule-worker": {"database"},
        "sender-worker": {"egress", "database"},
        "admin-web": {"egress", "database", "metrics"},
        "migrate": {"database"},
        "production-preflight": {"egress", "database", "metrics"},
        "production-activate": {"egress", "database", "metrics"},
    }
    for name, expected in expected_secrets.items():
        environment = services[name]["environment"]
        assert SECRET_ENVIRONMENTS.intersection(environment) == expected
        assert set(services[name]["networks"]) == expected_networks[name]


def test_public_roles_are_singleton_and_fail_closed() -> None:
    services = load_compose()["services"]
    for name in PUBLIC_ROLES:
        service = services[name]
        environment = service["environment"]
        assert service["deploy"] == {"replicas": 1}
        assert environment["ALERT_PROVIDER"] == "alerts_in_ua"
        assert environment["TELEGRAM_TRANSPORT"] == "live"
        assert environment["TEST_CHAT_ALLOWLIST"] == ""


def test_attestation_mount_is_fixed_and_read_only_for_every_application_action() -> None:
    services = load_compose()["services"]
    expected = {
        "type": "bind",
        "source": ATTESTATION_SOURCE,
        "target": "/run/time-tl-release",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    for name in APPLICATION_SERVICES:
        mounts = services[name]["volumes"]
        assert [
            mount
            for mount in mounts
            if isinstance(mount, dict) and mount.get("target") == "/run/time-tl-release"
        ] == [expected]


def test_admin_and_private_network_contract() -> None:
    compose = load_compose()
    services = compose["services"]
    admin_environment = services["admin-web"]["environment"]
    assert admin_environment["ADMIN_BACKEND"] == "postgres"
    assert admin_environment["ADMIN_COOKIE_SECURE"] == "true"
    assert services["admin-web"]["ports"] == [
        "${ADMIN_BIND_ADDRESS:-127.0.0.1}:${ADMIN_PORT:-8081}:8081"
    ]
    assert "egress" in services["admin-web"]["networks"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["node-exporter"]
    assert compose["networks"]["database"] == {"internal": True}
    assert compose["networks"]["metrics"] == {"internal": True}


def test_operator_commands_are_distinct_and_take_no_external_evidence() -> None:
    services = load_compose()["services"]
    revision = "${OWNER_CONFIG_REVISION:?set the signed ACTIVE revision}"
    assert services["production-preflight"]["command"] == [
        "preflight-production-owner-settings",
        "--revision",
        revision,
    ]
    assert services["production-activate"]["command"] == [
        "activate-production-owner-settings",
        "--revision",
        revision,
    ]
    assert "--evidence" not in PRODUCTION_COMPOSE.read_text(encoding="utf-8")


def test_runbook_requires_one_receipted_preflight_before_probe_free_activation() -> None:
    runbook = (ROOT / "docs" / "deployment" / "PORTAINER.md").read_text(
        encoding="utf-8"
    )
    assert "Run exactly one detached production preflight" in runbook
    assert "with a ten-minute expiry" in runbook
    assert "Activation makes no Telegram or provider request" in runbook
    assert "marks the receipt consumed" in runbook
    assert "handwritten evidence string" in runbook
