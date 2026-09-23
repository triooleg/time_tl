"""Command dispatcher shared by all application containers."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from enum import StrEnum

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
    parser.add_argument("service", choices=[item.value for item in Service] + ["bootstrap-admin"])
    parser.add_argument("--output", help=argparse.SUPPRESS)
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
