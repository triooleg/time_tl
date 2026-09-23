"""Safe one-time owner credential bootstrap."""

from __future__ import annotations

import getpass
import os
import secrets
import subprocess
from pathlib import Path

from time_tl.config import AdminBackend, Environment, Settings

from .repository import LocalAdminRepository
from .security import BOOTSTRAP_TTL, hash_password, utcnow


def bootstrap_admin(settings: Settings, output: str) -> None:
    if settings.admin_backend is not AdminBackend.LOCAL_SQLITE:
        raise RuntimeError("bootstrap requires ADMIN_BACKEND=local_sqlite")
    if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
        raise RuntimeError("local bootstrap is restricted to local/simulation")
    path = Path(output).resolve()
    state_dir = Path(settings.admin_state_dir).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("bootstrap output already exists")
    password = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, password.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
        else:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(R,W)"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        LocalAdminRepository(state_dir).create_bootstrap(
            hash_password(password), utcnow() + BOOTSTRAP_TTL, str(path)
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        password = ""  # best effort; Python cannot guarantee memory zeroization
