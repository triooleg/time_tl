"""Small dependency-free liveness server for container healthchecks."""

from __future__ import annotations

import asyncio
from contextlib import suppress


async def serve_health(host: str, port: int, service_name: str) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1024), timeout=1)
        body = f'{{"status":"alive","service":"{service_name}"}}\n'.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    async with server:
        await server.serve_forever()
