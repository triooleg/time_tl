from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from time_tl.entrypoints.main import main


def cli_settings() -> SimpleNamespace:
    return SimpleNamespace(log_level="INFO")


def test_production_preflight_cli_uses_baked_release_and_distinct_service(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = cli_settings()
    preflight = AsyncMock(return_value=SimpleNamespace(sanitized_summary=lambda: "safe-summary"))
    with (
        patch("time_tl.entrypoints.main.get_settings", return_value=settings),
        patch(
            "time_tl.entrypoints.operator_preflight.preflight_production_owner_settings",
            preflight,
        ),
    ):
        main(["preflight-production-owner-settings", "--revision", "12"])

    preflight.assert_awaited_once_with(settings, 12)
    assert capsys.readouterr().out.strip() == "safe-summary"


def test_production_preflight_cli_rejects_staging_allowlist_argument() -> None:
    with patch("time_tl.entrypoints.main.get_settings", return_value=cli_settings()):
        with pytest.raises(SystemExit, match="does not accept --approved-chat-id"):
            main(
                [
                    "preflight-production-owner-settings",
                    "--revision",
                    "12",
                    "--approved-chat-id",
                    "123",
                ]
            )


def test_production_activation_cli_routes_only_to_exact_production_path() -> None:
    settings = cli_settings()
    activation = AsyncMock()
    with (
        patch("time_tl.entrypoints.main.get_settings", return_value=settings),
        patch(
            "time_tl.runtime.services.activate_production_owner_settings",
            activation,
        ),
    ):
        main(
            [
                "activate-production-owner-settings",
                "--revision",
                "12",
            ]
        )

    activation.assert_awaited_once_with(settings, 12)


def test_production_cli_requires_revision_and_rejects_external_evidence() -> None:
    with patch("time_tl.entrypoints.main.get_settings", return_value=cli_settings()):
        with pytest.raises(SystemExit, match="production preflight requires --revision"):
            main(["preflight-production-owner-settings"])
        with pytest.raises(SystemExit, match="production activation requires --revision"):
            main(["activate-production-owner-settings"])
        with pytest.raises(SystemExit, match="does not accept --evidence"):
            main(
                [
                    "activate-production-owner-settings",
                    "--revision",
                    "12",
                    "--evidence",
                    "forged",
                ]
            )
