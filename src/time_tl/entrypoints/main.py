"""Command dispatcher shared by all application containers."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from time_tl.config import get_settings


class Service(StrEnum):
    BOT = "bot"
    ALERT_WORKER = "alert-worker"
    SCHEDULE_WORKER = "schedule-worker"
    SENDER_WORKER = "sender-worker"
    ADMIN_WEB = "admin-web"
    MIGRATE = "migrate"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="time-tl")
    parser.add_argument(
        "service",
        choices=[item.value for item in Service]
        + [
            "bootstrap-admin",
            "preflight-owner-settings",
            "activate-owner-settings",
            "import-territories",
            "activate-territories",
        ],
    )
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--revision", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--evidence", help=argparse.SUPPRESS)
    parser.add_argument("--approved-chat-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--katottg-json", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--catalog-version", help=argparse.SUPPRESS)
    parser.add_argument("--expected-source-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--expected-persisted-rows", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--expected-unresolved-total", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if args.service == "bootstrap-admin":
        output = args.output or settings.admin_bootstrap_artifact_path
        if not output:
            raise SystemExit("bootstrap-admin requires --output or ADMIN_BOOTSTRAP_ARTIFACT_PATH")
        from time_tl.runtime.services import run_admin_bootstrap

        run_admin_bootstrap(settings, output)
        return
    if args.service == "activate-owner-settings":
        if args.revision is None or not args.evidence:
            raise SystemExit("activation requires --revision and --evidence")
        from time_tl.runtime.services import activate_owner_settings

        asyncio.run(activate_owner_settings(settings, args.revision, args.evidence))
        return
    if args.service == "preflight-owner-settings":
        if args.revision is None or args.approved_chat_id is None:
            raise SystemExit("preflight requires --revision and --approved-chat-id")
        from time_tl.entrypoints.operator_preflight import preflight_owner_settings

        evidence = asyncio.run(
            preflight_owner_settings(settings, args.revision, args.approved_chat_id)
        )
        print(evidence.sanitized_summary())
        return
    if args.service == "import-territories":
        if args.katottg_json is None or not args.catalog_version:
            raise SystemExit("import-territories requires --katottg-json and --catalog-version")
        from time_tl.entrypoints.territory_import import import_territory_catalog

        result = asyncio.run(
            import_territory_catalog(
                settings,
                katottg_json=args.katottg_json,
                catalog_version=args.catalog_version,
            )
        )
        print(
            f"catalog={result.catalog_version} rows={result.row_count} "
            f"selectable={result.selectable_count} "
            f"hidden={result.hidden_nonselectable_count} "
            f"unresolved_total={result.unresolved_total} "
            f"source_sha256={result.source_sha256}"
        )
        return
    if args.service == "activate-territories":
        if (
            not args.catalog_version
            or not args.expected_source_sha256
            or args.expected_persisted_rows is None
            or args.expected_unresolved_total is None
        ):
            raise SystemExit(
                "activate-territories requires --catalog-version, "
                "--expected-source-sha256, --expected-persisted-rows, and "
                "--expected-unresolved-total"
            )
        from time_tl.entrypoints.territory_import import activate_territory_catalog

        activation_result = asyncio.run(
            activate_territory_catalog(
                settings,
                catalog_version=args.catalog_version,
                expected_source_sha256=args.expected_source_sha256,
                expected_persisted_rows=args.expected_persisted_rows,
                expected_unresolved_total=args.expected_unresolved_total,
            )
        )
        print(
            f"catalog={activation_result.catalog_version} "
            f"rows={activation_result.record_count} "
            f"unresolved_total={activation_result.unresolved_total} "
            f"source_sha256={activation_result.source_sha256} activated=true"
        )
        return
    if args.service == Service.ADMIN_WEB.value:
        from time_tl.runtime.services import run_admin_web_service

        run_admin_web_service(settings)
        return
    if args.service == Service.MIGRATE.value:
        from time_tl.runtime.services import run_migrations

        run_migrations(settings)
        return

    from time_tl.runtime.services import (
        run_alert_worker,
        run_bot,
        run_schedule_worker,
        run_sender_worker,
    )

    service = Service(args.service)
    if service is Service.ALERT_WORKER:
        asyncio.run(run_alert_worker(settings))
    elif service is Service.BOT:
        asyncio.run(run_bot(settings))
    elif service is Service.SCHEDULE_WORKER:
        asyncio.run(run_schedule_worker(settings))
    else:
        asyncio.run(run_sender_worker(settings))


if __name__ == "__main__":
    main()
