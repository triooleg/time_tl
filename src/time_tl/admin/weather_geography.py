"""Public, immutable geographic derivative; no account or operational catalogue data."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from fastapi import FastAPI
from fastapi.responses import Response

from time_tl.runtime import production_release, weather_release
from time_tl.runtime.contracts import RuntimeContractError

ROOT = Path("/run/time-tl-geography")
PUBLIC_FILES = frozenset(
    {
        "geography.csv",
        "ATTRIBUTION.md",
        "LICENSE.txt",
        "UPSTREAM-LICENSE.txt",
        "PROCEDURE.md",
        "prepare_weather_geography.py",
    }
)


def public_artifact(digest: str, filename: str, *, root: Path = ROOT) -> bytes:
    """Verify the original signed publication, also while running the fallback.

    This certificate authorizes public bytes only; it grants no runtime feature.
    Its primary image need not equal the currently executing fallback image.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or filename not in (
        PUBLIC_FILES | {"geography-manifest.json", "geography-bundle.zip"}
    ):
        raise ValueError("geography_unavailable")
    raw = weather_release._read(root / "weather-manifest.json", 65536)
    signature = weather_release._read(root / "weather-manifest.ed25519", 256)
    production_release._public_key().verify(base64.b64decode(signature.strip(), validate=True), raw)
    certificate = weather_release._parse(raw)
    if certificate["profile"] != "primary" or certificate["geography_sha256"] != digest:
        raise ValueError("geography_unavailable")
    metadata_raw = weather_release._read(root / "geography-manifest.json", 16384)
    if hashlib.sha256(metadata_raw).hexdigest() != certificate["geography_manifest_sha256"]:
        raise ValueError("geography_unavailable")
    metadata: dict[str, Any] = json.loads(metadata_raw, object_pairs_hook=weather_release._pairs)
    if (
        set(metadata["files"]) != PUBLIC_FILES
        or set(metadata["published_files"]) != PUBLIC_FILES
        or metadata["normalized_sha256"] != digest
        or metadata["files"]["geography.csv"] != digest
        or metadata["license"] != "ODbL-1.0"
        or metadata["operational_catalog_redistributed"] is not False
    ):
        raise ValueError("geography_unavailable")
    if filename == "geography-manifest.json":
        return metadata_raw
    content = weather_release._read(root / filename, 2_097_152)
    if filename == "geography-bundle.zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if (
                len(names) != len(PUBLIC_FILES) + 1
                or set(names) != PUBLIC_FILES | {"geography-manifest.json"}
                or sum(item.file_size for item in archive.infolist()) > 2_097_152
            ):
                raise ValueError("geography_unavailable")
            for name in names:
                data = archive.read(name)
                if name == "geography-manifest.json":
                    if data != metadata_raw:
                        raise ValueError("geography_unavailable")
                elif hashlib.sha256(data).hexdigest() != metadata["files"][name]:
                    raise ValueError("geography_unavailable")
    elif hashlib.sha256(content).hexdigest() != metadata["files"][filename]:
        raise ValueError("geography_unavailable")
    return content


def register_geography(app: FastAPI) -> None:
    @app.get("/weather/geography/{digest}/{filename}")
    async def geography(digest: str, filename: str) -> Response:
        try:
            content = public_artifact(digest, filename)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            InvalidSignature,
            RuntimeContractError,
            zipfile.BadZipFile,
        ):
            return Response(status_code=404)
        media = (
            "application/zip"
            if filename.endswith(".zip")
            else "application/json"
            if filename.endswith(".json")
            else "text/csv"
            if filename.endswith(".csv")
            else "text/plain"
        )
        return Response(
            content,
            media_type=media,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
