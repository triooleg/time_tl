"""Protected one-time bootstrap credential for the async PostgreSQL admin backend."""

from __future__ import annotations

import asyncio
import getpass
import os
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .security import BOOTSTRAP_TTL, hash_password, utcnow


class BootstrapRepository(Protocol):
    async def create_bootstrap(
        self, password_hash: str, expires_at: datetime, username: str = "admin"
    ) -> None: ...


def _create_protected_output(output: str | Path, password: str) -> tuple[Path, str]:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("bootstrap output already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, password.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        if os.name == "nt":
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(R,W)"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.chmod(path, 0o600)
        return path, hash_password(password)
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def bootstrap_async_admin(repository: BootstrapRepository, output: str | Path) -> Path:
    """Create a protected credential file and persist only its Argon2id hash."""

    password = secrets.token_urlsafe(24)
    path: Path | None = None
    try:
        path, password_hash = await asyncio.to_thread(_create_protected_output, output, password)
        await repository.create_bootstrap(password_hash, utcnow() + BOOTSTRAP_TTL, username="admin")
        assert path is not None
        return path
    except Exception:
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        raise
    finally:
        password = ""
