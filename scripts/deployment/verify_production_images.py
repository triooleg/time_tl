"""Fail closed unless production image inputs are exact immutable digest references."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

IMAGE_ENVIRONMENTS = {
    "TIME_TL_PRODUCTION_IMAGE": {
        "bot",
        "alert-worker",
        "schedule-worker",
        "sender-worker",
        "admin-web",
        "migrate",
        "production-preflight",
        "production-activate",
    },
    "TIME_TL_POSTGRES_IMAGE": {"postgres"},
    "TIME_TL_NODE_EXPORTER_IMAGE": {"node-exporter"},
}

_NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
_IMAGE_REFERENCE = re.compile(
    rf"{_NAME_COMPONENT}(?::[0-9]+)?(?:/{_NAME_COMPONENT})*@sha256:[0-9a-f]{{64}}\Z"
)
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


class VerificationError(ValueError):
    """Raised for a secret-free production image contract failure."""


def is_digest_pinned_image(value: str) -> bool:
    return _IMAGE_REFERENCE.fullmatch(value) is not None


def read_environment(path: Path) -> dict[str, str]:
    """Parse a dotenv file without expanding or logging any value."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise VerificationError("protected environment file is unavailable") from None
    result: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise VerificationError(f"protected environment line {number} is malformed")
        if name in result:
            raise VerificationError(f"protected environment contains duplicate {name}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[name] = value
    return result


def verify_environment_images(environment: dict[str, str]) -> dict[str, str]:
    verified: dict[str, str] = {}
    for name in IMAGE_ENVIRONMENTS:
        value = environment.get(name, "")
        if not is_digest_pinned_image(value):
            raise VerificationError(
                f"{name} must be repository@sha256 followed by 64 lowercase hex characters"
            )
        verified[name] = value
    return verified


def verify_compose_config(path: Path, expected: dict[str, str]) -> None:
    """Verify Docker Compose's resolved JSON uses exactly the protected references."""

    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError("rendered Compose JSON is unavailable or malformed") from None
    if not isinstance(value, dict) or not isinstance(value.get("services"), dict):
        raise VerificationError("rendered Compose JSON has no services object")
    services = value["services"]
    expected_services = set().union(*IMAGE_ENVIRONMENTS.values())
    if set(services) != expected_services:
        raise VerificationError("rendered Compose service set does not match production contract")
    for environment_name, service_names in IMAGE_ENVIRONMENTS.items():
        reference = expected[environment_name]
        for service_name in service_names:
            service = services.get(service_name)
            if not isinstance(service, dict) or service.get("image") != reference:
                raise VerificationError(
                    f"rendered Compose image mismatch for service {service_name}"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--compose-config-json", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        expected = verify_environment_images(read_environment(args.env_file))
        if args.compose_config_json is not None:
            verify_compose_config(args.compose_config_json, expected)
    except VerificationError as exc:
        print(f"production image verification: REJECTED: {exc}")
        return 2
    scope = "environment+compose" if args.compose_config_json is not None else "environment"
    print(f"production image verification: PASS ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
