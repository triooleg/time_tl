from __future__ import annotations

from typing import Protocol


class LessonLinkCodec(Protocol):
    def encrypt(self, value: str) -> bytes: ...
    def decrypt(self, value: bytes) -> str: ...


class FernetLessonLinkCodec:
    """Authenticated encryption; requires the `cryptography` runtime package."""

    def __init__(self, key: bytes) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError("cryptography is required for lesson link encryption") from exc
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        return self._fernet.decrypt(value).decode("utf-8")
