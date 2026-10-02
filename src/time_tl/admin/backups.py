"""Read-only public backup catalogue and owner-facing Russian presentation."""
# ruff: noqa: E501  # embedded server-rendered HTML, escaped public fields

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_CATALOGUE = Path("/run/time-tl-release/backup-catalogue.json")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")
IMAGE = re.compile(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}\Z")


class BackupCatalogueUnavailable(ValueError):
    """Public metadata is absent or cannot be trusted."""


def _time(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise BackupCatalogueUnavailable()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed > now:
        raise BackupCatalogueUnavailable()
    return parsed


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise BackupCatalogueUnavailable()
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise BackupCatalogueUnavailable()
        result[key] = value
    return result


class BackupCatalogue:
    def __init__(self, path: Path = DEFAULT_CATALOGUE) -> None:
        self.path = path

    def read(self, now: datetime) -> dict[str, Any]:
        try:
            if any(path.is_symlink() for path in (self.path, *self.path.parents)):
                raise BackupCatalogueUnavailable()
            fd = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or not 1 <= meta.st_size <= 262144:
                    raise BackupCatalogueUnavailable()
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise BackupCatalogueUnavailable()
            value = json.loads(raw, object_pairs_hook=_pairs)
            keys = {
                "schema",
                "generated_at",
                "version",
                "retention_days",
                "active_revision",
                "application_image",
                "backup_source_sha256",
                "entries",
            }
            if (
                not isinstance(value, dict)
                or set(value) != keys
                or type(value["schema"]) is not int
                or value["schema"] != 1
            ):
                raise BackupCatalogueUnavailable()
            generated = _time(value["generated_at"], now)
            if now - generated > timedelta(minutes=5):
                raise BackupCatalogueUnavailable()
            _integer(value["retention_days"], 1, 90)
            _integer(value["active_revision"], 1, 2**31 - 1)
            if not isinstance(value["application_image"], str) or not IMAGE.fullmatch(
                value["application_image"]
            ):
                raise BackupCatalogueUnavailable()
            for key in ("version", "backup_source_sha256"):
                if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
                    raise BackupCatalogueUnavailable()
            payload = {
                key: item for key, item in value.items() if key not in {"generated_at", "version"}
            }
            digest = hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
            if digest != value["version"]:
                raise BackupCatalogueUnavailable()
            entries = value["entries"]
            if not isinstance(entries, list) or len(entries) > 500:
                raise BackupCatalogueUnavailable()
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {
                    "id",
                    "snapshot_at",
                    "verified_at",
                    "size_bytes",
                    "sha256",
                    "schema",
                    "restore",
                }:
                    raise BackupCatalogueUnavailable()
                identifier = entry["id"]
                if (
                    not isinstance(identifier, str)
                    or not ID.fullmatch(identifier)
                    or identifier in seen
                ):
                    raise BackupCatalogueUnavailable()
                seen.add(identifier)
                if _time(entry["snapshot_at"], now) > _time(entry["verified_at"], now):
                    raise BackupCatalogueUnavailable()
                _integer(entry["size_bytes"], 1, 2**50)
                if not isinstance(entry["sha256"], str) or not HEX64.fullmatch(entry["sha256"]):
                    raise BackupCatalogueUnavailable()
                if (
                    not isinstance(entry["schema"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", entry["schema"])
                    or entry["restore"] != "PASS"
                ):
                    raise BackupCatalogueUnavailable()
            return value
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise BackupCatalogueUnavailable() from exc


def public_control(value: dict[str, Any]) -> dict[str, Any]:
    """Never forward unbounded repository/audit fields into the browser."""
    result: dict[str, Any] = {
        "version": _integer(value["version"], 0, 2**31 - 1),
        "retention_days": _integer(value["retention_days"], 1, 90),
        "request": None,
    }
    request = value.get("request")
    if isinstance(request, dict):
        status = request.get("status")
        if status not in {
            "PENDING",
            "CLAIMED",
            "PASS",
            "FAILED",
            "REQUIRES_OPERATOR",
            "EXPIRED",
            "REJECTED",
            "CANCELLED",
        }:
            status = "UNKNOWN"
        result["request"] = {
            "kind": request.get("kind")
            if request.get("kind") in {"verify", "restore"}
            else "unknown",
            "artifact_id": request.get("artifact_id")
            if isinstance(request.get("artifact_id"), str) and ID.fullmatch(request["artifact_id"])
            else None,
            "status": status,
            "request_id": request.get("request_id")
            if isinstance(request.get("request_id"), str)
            and re.fullmatch(r"[0-9a-f-]{32,36}", request["request_id"])
            else None,
        }
    return result


def backups_html(
    csrf: str, catalogue: dict[str, Any], control: dict[str, Any], message: str = ""
) -> str:
    escape = html.escape
    hidden = f'<input type="hidden" name="csrf" value="{escape(csrf)}"><input type="hidden" name="expected_version" value="{control["version"]}">'
    password = '<label>Текущий пароль<input type="password" name="current_password" autocomplete="current-password" required></label>'
    rows, options = [], []
    for entry in catalogue["entries"]:
        stamp = (
            datetime.fromisoformat(entry["snapshot_at"])
            .astimezone(ZoneInfo("Europe/Kyiv"))
            .strftime("%d.%m.%Y %H:%M")
        )
        identifier = escape(entry["id"])
        rows.append(
            f"<tr><td>{identifier}</td><td>{stamp} (Киев)</td><td>{entry['size_bytes']} байт</td><td>{entry['sha256'][:12]}…</td><td>Проверена · PASS</td></tr>"
        )
        options.append(f'<option value="{identifier}">{stamp} · {identifier}</option>')
    selected = (
        f'<label>Копия<select name="artifact_id" required>{"".join(options)}</select></label>'
    )
    binding = f'<input type="hidden" name="catalogue_version" value="{catalogue["version"]}">'
    request = control["request"]
    labels = {
        "PENDING": "Ожидает выполнения",
        "CLAIMED": "Выполняется",
        "PASS": "Проверка пройдена (изолированное восстановление)",
        "REQUIRES_OPERATOR": "Подготовлено: требуется оператор",
        "REJECTED": "Отклонено",
        "FAILED": "Ошибка проверки",
        "CANCELLED": "Отменено",
        "EXPIRED": "Срок запроса истёк",
    }
    status = labels.get(request["status"], "Состояние недоступно") if request else "Нет запросов"
    notice = f'<p role="status">{escape(message)}</p>' if message else ""
    notice += "<p>Проверка копии восстанавливает данные только в изолированной временной базе. Production не заменяется.</p>"
    if control["retention_days"] != catalogue["retention_days"]:
        notice += f'<p role="status">Новый срок {control["retention_days"]} дней ожидает применения. Сервер пока подтвердил {catalogue["retention_days"]} дней.</p>'
    if request and request["request_id"] and request["status"] in {"PENDING", "REQUIRES_OPERATOR"}:
        notice += f'<form method="post" action="/backups/cancel">{hidden}<input type="hidden" name="request_id" value="{escape(request["request_id"])}">{password}<button>Отменить запрос и снять защиту выбранной копии</button></form>'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Резервные копии</title><link rel="stylesheet" href="/static/admin.css"></head><body><main><a href="/">Вернуться в панель</a><h1>Резервные копии</h1>{notice}<p>Все проверенные копии за последние {control["retention_days"]} дней. Частота создания: каждые 6 часов. Срок хранения не отключает защиту свежести копии.</p><section><h2>Хранение</h2><p>Сокращение срока удалит старые проверенные копии при следующем успешном цикле. Выбранные копии защищены от удаления.</p><form method="post" action="/settings/backups">{hidden}<label>Срок хранения, дней<input type="number" name="retention_days" min="1" max="90" value="{control["retention_days"]}" required></label><label><input type="checkbox" name="cleanup_ack" value="yes" required>Понимаю последствия сокращения срока хранения</label>{password}<button>Сохранить срок хранения</button></form></section><section><h2>Доступные копии</h2><table><caption>Проверенные копии</caption><thead><tr><th>ID</th><th>Дата снимка</th><th>Размер</th><th>SHA-256</th><th>Проверка восстановления</th></tr></thead><tbody>{"".join(rows)}</tbody></table><p>{"" if rows else "Проверенных копий пока нет."}</p></section><section><h2>Операции</h2><p>Состояние последнего запроса: {status}. Проверка выполняется операторским заданием, не браузером.</p><form method="post" action="/backups/verify">{hidden}{binding}{selected}{password}<button{"" if options else " disabled"}>Проверить выбранную копию</button></form><h2>Подготовить восстановление</h2><p class="critical">Все изменения после даты снимка будут потеряны при восстановлении. Эта кнопка только подготавливает запрос: требуется оператор, отдельное обслуживание и подтверждение. Боевая база здесь не заменяется.</p><form method="post" action="/backups/restore-request">{hidden}{binding}{selected}<label>Подтверждение: ВОССТАНОВИТЬ и полный ID копии<input name="restore_ack" required autocomplete="off"></label><label><input type="checkbox" name="data_loss_ack" value="yes" required>Понимаю, что изменения после снимка будут потеряны</label>{password}<button{"" if options else " disabled"}>Подготовить восстановление</button></form></section></main></body></html>'''
