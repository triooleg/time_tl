from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from time_tl.entrypoints import main as cli
from time_tl.entrypoints import territory_import
from time_tl.infrastructure.db.territories import TerritoryDraftRow
from time_tl.runtime.territory_catalog_builder import TerritoryCatalogBuildResult

SOURCE_SHA256 = "a" * 64


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        database_url=SecretStr("postgresql+asyncpg://ignored"),
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_second_step_activation_uses_existing_draft_without_loading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    calls: list[dict[str, Any]] = []

    class FakeRepository:
        def __init__(self, factory: object) -> None:
            assert factory == "factory"

        async def activate(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        async def load_draft(self, **_kwargs: Any) -> int:
            raise AssertionError("the activation step must not reload or replace the draft")

    monkeypatch.setattr(territory_import, "create_engine", lambda _url: engine)
    monkeypatch.setattr(territory_import, "create_session_factory", lambda _candidate: "factory")
    monkeypatch.setattr(territory_import, "PostgresTerritoryCatalogRepository", FakeRepository)

    result = await territory_import.activate_territory_catalog(
        settings(),
        catalog_version="katottg-2026-09-24",
        expected_source_sha256=SOURCE_SHA256,
        expected_persisted_rows=1327,
        expected_unresolved_total=295,
    )

    assert calls == [
        {
            "provider": "alerts.in.ua",
            "catalog_version": "katottg-2026-09-24",
            "expected_source_sha256": SOURCE_SHA256,
            "expected_record_count": 1327,
            "expected_unresolved_total": 295,
        }
    ]
    assert result.record_count == 1327
    assert result.unresolved_total == 295
    assert engine.disposed


@pytest.mark.asyncio
async def test_import_without_activate_persists_draft_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeEngine()
    calls: list[dict[str, Any]] = []
    source = tmp_path / "katottg.json"
    source.write_bytes(b"official snapshot")
    built = TerritoryCatalogBuildResult(
        rows=(
            TerritoryDraftRow(
                region_id="1",
                name="Oblast",
                kind="OBLAST",
                parent_region_id=None,
                external_code="UA00000000000000001",
            ),
        ),
        unresolved=(),
        alerts_source_sha256="b" * 64,
        katottg_source_sha256="c" * 64,
        source_sha256=SOURCE_SHA256,
        source_revision="2026-09-24",
    )

    class FakeRepository:
        def __init__(self, _factory: object) -> None:
            pass

        async def load_draft(self, **kwargs: Any) -> int:
            calls.append(kwargs)
            return len(kwargs["rows"])

        async def activate(self, **_kwargs: Any) -> None:
            raise AssertionError("plain import must remain a DRAFT-only operation")

    monkeypatch.setattr(territory_import, "build_territory_catalog", lambda _data: built)
    monkeypatch.setattr(territory_import, "create_engine", lambda _url: engine)
    monkeypatch.setattr(territory_import, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(territory_import, "PostgresTerritoryCatalogRepository", FakeRepository)

    result = await territory_import.import_territory_catalog(
        settings(),
        katottg_json=source,
        catalog_version="katottg-2026-09-24",
    )

    assert len(calls) == 1
    assert result.source_sha256 == SOURCE_SHA256
    assert result.row_count == 1
    assert result.selectable_count == 1
    assert result.hidden_nonselectable_count == 0
    assert result.unresolved_total == 0
    assert engine.disposed


def test_import_command_rejects_the_removed_activate_shortcut() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "import-territories",
                "--katottg-json",
                "katottg.json",
                "--catalog-version",
                "v1",
                "--activate",
            ]
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["activate-territories", "--catalog-version", "v1"],
        [
            "activate-territories",
            "--catalog-version",
            "v1",
            "--expected-source-sha256",
            SOURCE_SHA256,
            "--expected-persisted-rows",
            "1327",
        ],
    ],
)
def test_activate_command_requires_complete_reviewed_evidence(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", settings)

    with pytest.raises(SystemExit, match="requires --catalog-version"):
        cli.main(argv)
