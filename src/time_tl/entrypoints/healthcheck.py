"""Container healthcheck client."""

from __future__ import annotations

import os
import socket


def main() -> int:
    host = "127.0.0.1"
    port = int(os.getenv("HEALTH_PORT", "8080"))
    path = os.getenv("HEALTH_PATH", "/healthz")
    if not path.startswith("/") or any(char in path for char in "\r\n "):
        return 1
    try:
        with socket.create_connection((host, port), timeout=2) as connection:
            request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            connection.sendall(request.encode("ascii"))
            response = connection.recv(64)
    except (OSError, UnicodeEncodeError):
        return 1
    return 0 if response.startswith(b"HTTP/1.1 200") else 1


if __name__ == "__main__":
    raise SystemExit(main())
