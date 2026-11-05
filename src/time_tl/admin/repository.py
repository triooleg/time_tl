"""SQLite repository for a local-only owner panel.

This backend is deliberately rejected by configuration in staging/production.
"""
# ruff: noqa: E501  # audited SQL literals

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import (
    FAILURE_WINDOW,
    LOCKOUT_AFTER,
    LOCKOUT_DURATION,
    SESSION_ABSOLUTE_TTL,
    Session,
    new_token,
    token_hash,
    utcnow,
)
from .settings import reject_sensitive_generic_settings


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str
    must_change_password: bool
    bootstrap_expires_at: datetime | None
    bootstrap_file: str | None


class LocalAdminRepository:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "admin.sqlite3"
        self._init_schema()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(
              username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
              must_change INTEGER NOT NULL, bootstrap_expires_at TEXT,
              bootstrap_file TEXT, changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_failures(
              username TEXT NOT NULL, source_ip TEXT NOT NULL, attempted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_failures_lookup
              ON login_failures(username, source_ip, attempted_at);
            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, username TEXT NOT NULL,
              created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
              absolute_expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, owner TEXT NOT NULL,
              action TEXT NOT NULL, result TEXT NOT NULL, before_redacted TEXT, after_redacted TEXT
            );
            """)

    def account(self, username: str = "admin") -> Account | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        expiry = _dt(row["bootstrap_expires_at"]) if row["bootstrap_expires_at"] else None
        return Account(
            row["username"],
            row["password_hash"],
            bool(row["must_change"]),
            expiry,
            row["bootstrap_file"],
        )

    def create_bootstrap(self, password_hash: str, expires_at: datetime, file_path: str) -> None:
        with self._db() as db:
            if db.execute("SELECT 1 FROM accounts WHERE username='admin'").fetchone():
                raise RuntimeError("admin account already exists")
            db.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,?,?)",
                (
                    "admin",
                    password_hash,
                    1,
                    expires_at.isoformat(),
                    file_path,
                    utcnow().isoformat(),
                ),
            )

    def bootstrap_is_valid(self, account: Account, now: datetime) -> bool:
        if not account.must_change_password or account.bootstrap_expires_at is None:
            return True
        if now < account.bootstrap_expires_at:
            return True
        self.destroy_bootstrap_file(account.bootstrap_file)
        return False

    @staticmethod
    def destroy_bootstrap_file(file_path: str | None) -> None:
        if not file_path:
            return
        path = Path(file_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def change_password(self, username: str, password_hash: str) -> None:
        account = self.account(username)
        if not account:
            raise LookupError(username)
        with self._db() as db:
            db.execute(
                "UPDATE accounts SET password_hash=?, must_change=0, bootstrap_expires_at=NULL, bootstrap_file=NULL, changed_at=? WHERE username=?",
                (password_hash, utcnow().isoformat(), username),
            )
            db.execute("DELETE FROM sessions WHERE username=?", (username,))
        self.destroy_bootstrap_file(account.bootstrap_file)

    def lockout_remaining(self, username: str, source_ip: str, now: datetime) -> timedelta | None:
        cutoff = now - FAILURE_WINDOW
        with self._db() as db:
            db.execute(
                "DELETE FROM login_failures WHERE attempted_at < ?",
                ((now - timedelta(days=1)).isoformat(),),
            )
            rows = db.execute(
                "SELECT attempted_at FROM login_failures WHERE username=? AND source_ip=? AND attempted_at>=? ORDER BY attempted_at",
                (username, source_ip, cutoff.isoformat()),
            ).fetchall()
        if len(rows) < LOCKOUT_AFTER:
            return None
        locked_until = _dt(rows[-1]["attempted_at"]) + LOCKOUT_DURATION
        return max(locked_until - now, timedelta()) if now < locked_until else None

    def record_failure(self, username: str, source_ip: str, now: datetime) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO login_failures VALUES(?,?,?)", (username, source_ip, now.isoformat())
            )

    def reset_failures(self, username: str, source_ip: str) -> None:
        with self._db() as db:
            db.execute(
                "DELETE FROM login_failures WHERE username=? AND source_ip=?", (username, source_ip)
            )

    def create_session(self, username: str, must_change: bool, now: datetime) -> Session:
        token, csrf = new_token(), new_token()
        session = Session(token, csrf, username, must_change, now, now, now + SESSION_ABSOLUTE_TTL)
        with self._db() as db:
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                (
                    token_hash(token),
                    csrf,
                    username,
                    now.isoformat(),
                    now.isoformat(),
                    session.absolute_expires_at.isoformat(),
                ),
            )
        return session

    def get_session(self, token: str, now: datetime, touch: bool = True) -> Session | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE token_hash=?", (token_hash(token),)
            ).fetchone()
            if not row:
                return None
            account = db.execute(
                "SELECT must_change FROM accounts WHERE username=?", (row["username"],)
            ).fetchone()
            session = Session(
                token,
                row["csrf_token"],
                row["username"],
                bool(account[0]),
                _dt(row["created_at"]),
                _dt(row["last_seen_at"]),
                _dt(row["absolute_expires_at"]),
            )
            if session.expired(now):
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
                return None
            if touch:
                db.execute(
                    "UPDATE sessions SET last_seen_at=? WHERE token_hash=?",
                    (now.isoformat(), token_hash(token)),
                )
        return session

    def delete_session(self, token: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def delete_other_sessions(self, username: str, current_token: str) -> None:
        with self._db() as db:
            db.execute(
                "DELETE FROM sessions WHERE username=? AND token_hash<>?",
                (username, token_hash(current_token)),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        reject_sensitive_generic_settings((key,))
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_settings(self, values: dict[str, Any], owner: str, action: str) -> None:
        reject_sensitive_generic_settings(values)
        before = {key: self.get_setting(key) for key in values}
        with self._db() as db:
            for key, value in values.items():
                db.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
            db.execute(
                "INSERT INTO audit(at,owner,action,result,before_redacted,after_redacted) VALUES(?,?,?,?,?,?)",
                (
                    utcnow().isoformat(),
                    owner,
                    action,
                    "success",
                    json.dumps({k: "changed" for k in before}),
                    json.dumps({k: "changed" for k in values}),
                ),
            )
