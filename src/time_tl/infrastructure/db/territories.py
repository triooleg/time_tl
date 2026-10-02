"""Versioned territory hierarchy persisted and served entirely from PostgreSQL.

Network retrieval and provider/KATOTTG reconciliation intentionally live outside
this repository.  Only a fully prepared draft can enter the database, and only a
fully validated draft can become the active read model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    ActiveTerritoryCatalog,
    AlertEpisode,
    AlertPollState,
    AlertScope,
    AlertSnapshot,
    Territory,
    TerritoryCatalogVersion,
)

ALLOWED_KINDS = frozenset({"OBLAST", "SPECIAL_STATUS_CITY", "RAION", "HROMADA"})
ROOT_KINDS = frozenset({"OBLAST", "SPECIAL_STATUS_CITY"})
PARENT_KIND: dict[str, str] = {"RAION": "OBLAST", "HROMADA": "RAION"}


class TerritoryCatalogValidationError(ValueError):
    """A candidate hierarchy is unsafe to publish."""


class TerritoryRuntimeNotReadyError(RuntimeError):
    """Production hierarchy state is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class TerritoryDraftRow:
    region_id: str
    name: str
    kind: str
    parent_region_id: str | None
    external_code: str | None = None
    sort_key: str = ""
    is_selectable: bool = True


@dataclass(frozen=True, slots=True)
class TerritoryNode:
    region_id: str
    name: str
    kind: str
    parent_region_id: str | None
    external_code: str | None
    catalog_version: str
    is_selectable: bool


@dataclass(frozen=True, slots=True)
class ResolvedTerritoryScope:
    provider: str
    catalog_version: str
    selected_region_id: str
    applicable_region_ids: tuple[str, ...]
    path: tuple[TerritoryNode, ...]


def validate_territory_rows(rows: Iterable[TerritoryDraftRow]) -> tuple[TerritoryDraftRow, ...]:
    """Normalize and validate a strict oblast -> raion -> hromada forest."""

    normalized: list[TerritoryDraftRow] = []
    by_id: dict[str, TerritoryDraftRow] = {}
    external_codes: set[str] = set()
    for raw in rows:
        row = TerritoryDraftRow(
            region_id=raw.region_id.strip(),
            name=raw.name.strip(),
            kind=raw.kind.strip().upper(),
            parent_region_id=(raw.parent_region_id.strip() if raw.parent_region_id else None),
            external_code=(raw.external_code.strip() if raw.external_code else None),
            sort_key=(raw.sort_key.strip() or raw.name.strip().casefold()),
            is_selectable=raw.is_selectable,
        )
        if not row.region_id or not row.name:
            raise TerritoryCatalogValidationError("territory UID and name must be non-empty")
        if row.kind not in ALLOWED_KINDS:
            raise TerritoryCatalogValidationError(
                f"territory {row.region_id!r} has unsupported kind {row.kind!r}"
            )
        if row.region_id in by_id:
            raise TerritoryCatalogValidationError(f"duplicate territory UID {row.region_id!r}")
        if row.external_code and row.external_code in external_codes:
            raise TerritoryCatalogValidationError(
                f"duplicate external territory code {row.external_code!r}"
            )
        by_id[row.region_id] = row
        if row.external_code:
            external_codes.add(row.external_code)
        normalized.append(row)

    if not normalized:
        raise TerritoryCatalogValidationError("territory catalogue cannot be empty")

    for row in normalized:
        if row.parent_region_id is not None and row.parent_region_id not in by_id:
            raise TerritoryCatalogValidationError(
                f"territory {row.region_id!r} references missing parent {row.parent_region_id!r}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(region_id: str) -> None:
        if region_id in visiting:
            raise TerritoryCatalogValidationError(
                f"territory hierarchy contains a cycle at {region_id!r}"
            )
        if region_id in visited:
            return
        visiting.add(region_id)
        parent_id = by_id[region_id].parent_region_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(region_id)
        visited.add(region_id)

    for region_id in by_id:
        visit(region_id)

    for row in normalized:
        if row.kind in ROOT_KINDS:
            if row.parent_region_id is not None:
                raise TerritoryCatalogValidationError(
                    f"root territory {row.region_id!r} cannot have a parent"
                )
            continue
        if row.parent_region_id is None:
            raise TerritoryCatalogValidationError(
                f"territory {row.region_id!r} is missing its parent"
            )
        parent = by_id[row.parent_region_id]
        expected = PARENT_KIND[row.kind]
        if parent.kind != expected:
            raise TerritoryCatalogValidationError(
                f"territory {row.region_id!r} ({row.kind}) requires parent kind "
                f"{expected}, got {parent.kind}"
            )

    for row in normalized:
        if not row.is_selectable:
            continue
        if row.external_code is None:
            raise TerritoryCatalogValidationError(
                f"selectable territory {row.region_id!r} has no external code"
            )
        ancestor_id = row.parent_region_id
        while ancestor_id is not None:
            ancestor = by_id[ancestor_id]
            if not ancestor.is_selectable or ancestor.external_code is None:
                raise TerritoryCatalogValidationError(
                    f"selectable territory {row.region_id!r} has unresolved ancestor "
                    f"{ancestor.region_id!r}"
                )
            ancestor_id = ancestor.parent_region_id

    child_kinds: dict[str, set[str]] = {}
    for row in normalized:
        if row.parent_region_id is not None:
            child_kinds.setdefault(row.parent_region_id, set()).add(row.kind)
    for row in normalized:
        # Some provider top-level territories currently expose no child UIDs
        # (for example occupied Crimea). They remain selectable at oblast level.
        if row.kind == "RAION" and "HROMADA" not in child_kinds.get(row.region_id, set()):
            raise TerritoryCatalogValidationError(
                f"raion {row.region_id!r} has no hromada and cannot be published"
            )

    depth = {"OBLAST": 0, "SPECIAL_STATUS_CITY": 0, "RAION": 1, "HROMADA": 2}
    return tuple(sorted(normalized, key=lambda row: (depth[row.kind], row.sort_key, row.region_id)))


class PostgresTerritoryCatalogRepository:
    """Stage, validate, atomically activate, and query territory catalogues."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        expected_region_ids: frozenset[str] | None = None,
    ) -> None:
        self._factory = factory
        self._expected_region_ids = expected_region_ids

    async def assert_runtime_ready(
        self,
        *,
        provider: str,
        expected_region_ids: frozenset[str] | None = None,
    ) -> str:
        """Fail closed unless the active catalogue and every scope binding agree."""

        async with self._factory() as session:
            async with session.begin():
                return await self.assert_runtime_ready_in_session(
                    session,
                    provider=provider,
                    expected_region_ids=expected_region_ids,
                )

    async def assert_runtime_ready_in_session(
        self,
        session: AsyncSession,
        *,
        provider: str,
        expected_region_ids: frozenset[str] | None = None,
    ) -> str:
        expected_region_ids = self._resolve_expected_region_ids(expected_region_ids)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:provider, 0))"),
            {"provider": provider},
        )
        pointer = (
            await session.execute(
                select(ActiveTerritoryCatalog)
                .where(ActiveTerritoryCatalog.provider == provider)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if pointer is None:
            raise TerritoryRuntimeNotReadyError("active territory catalogue is missing")
        version = (
            await session.execute(
                select(TerritoryCatalogVersion).where(
                    TerritoryCatalogVersion.provider == provider,
                    TerritoryCatalogVersion.catalog_version == pointer.catalog_version,
                )
            )
        ).scalar_one_or_none()
        if version is None or version.status != "ACTIVE":
            raise TerritoryRuntimeNotReadyError(
                "active territory pointer targets a missing or non-active revision"
            )
        rows = list(
            (
                await session.execute(
                    select(Territory)
                    .where(
                        Territory.provider == provider,
                        Territory.catalog_version == pointer.catalog_version,
                    )
                    .order_by(Territory.region_id)
                    .with_for_update()
                )
            ).scalars()
        )
        try:
            prepared = validate_territory_rows(self._draft(row) for row in rows)
        except TerritoryCatalogValidationError as error:
            raise TerritoryRuntimeNotReadyError(
                "active territory catalogue hierarchy is invalid"
            ) from error
        actual_count = len(prepared)
        actual_selectable = sum(row.is_selectable for row in prepared)
        actual_region_ids = frozenset(row.region_id for row in prepared)
        if expected_region_ids is not None and actual_region_ids != expected_region_ids:
            raise TerritoryRuntimeNotReadyError(
                "active territory catalogue UIDs differ from the official provider registry"
            )
        report_unresolved = version.reconciliation_report.get("unresolved_total")
        if (
            actual_count <= 0
            or actual_count != version.record_count
            or actual_selectable != version.selectable_count
            or actual_count - actual_selectable != version.hidden_nonselectable_count
            or version.selectable_count + version.hidden_nonselectable_count != version.record_count
            or version.unresolved_total < version.hidden_nonselectable_count
            or report_unresolved != version.unresolved_total
        ):
            raise TerritoryRuntimeNotReadyError(
                "active territory catalogue count metadata is inconsistent"
            )
        scopes = list(
            (
                await session.execute(select(AlertScope).order_by(AlertScope.id).with_for_update())
            ).scalars()
        )
        for scope in scopes:
            if (
                scope.territory_provider != provider
                or scope.territory_catalog_version != pointer.catalog_version
            ):
                raise TerritoryRuntimeNotReadyError(
                    "alert scopes are not fully bound to the active territory catalogue"
                )
            try:
                canonical = list(
                    self._scope_closure_from_rows(
                        rows,
                        catalog_version=pointer.catalog_version,
                        region_id=scope.selected_region_id,
                    )
                )
            except LookupError as error:
                raise TerritoryRuntimeNotReadyError(
                    "alert scope selects a missing or nonselectable territory"
                ) from error
            if scope.applicable_region_ids != canonical:
                raise TerritoryRuntimeNotReadyError(
                    "alert scope hierarchy closure differs from the active catalogue"
                )
        return pointer.catalog_version

    async def load_draft(
        self,
        *,
        provider: str,
        catalog_version: str,
        source_revision: str,
        source_sha256: str,
        rows: Iterable[TerritoryDraftRow],
        selectable_count: int | None = None,
        hidden_nonselectable_count: int | None = None,
        unresolved_total: int | None = None,
        reconciliation_report: Mapping[str, Any] | None = None,
    ) -> int:
        provider = provider.strip()
        catalog_version = catalog_version.strip()
        source_revision = source_revision.strip()
        source_sha256 = source_sha256.strip().lower()
        if not provider or not catalog_version or not source_revision:
            raise TerritoryCatalogValidationError(
                "provider, catalog version, and source revision are required"
            )
        if len(source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_sha256):
            raise TerritoryCatalogValidationError("source SHA256 must be 64 lowercase hex digits")
        prepared = validate_territory_rows(rows)
        if not self._region_ids_match(prepared, self._expected_region_ids):
            raise TerritoryCatalogValidationError(
                "draft territory UIDs differ from the official provider registry"
            )
        computed_selectable = sum(row.is_selectable for row in prepared)
        computed_unresolved = len(prepared) - computed_selectable
        if selectable_count is not None and selectable_count != computed_selectable:
            raise TerritoryCatalogValidationError(
                "supplied selectable count differs from validated catalogue"
            )
        if (
            hidden_nonselectable_count is not None
            and hidden_nonselectable_count != computed_unresolved
        ):
            raise TerritoryCatalogValidationError(
                "supplied hidden nonselectable count differs from validated catalogue"
            )
        report = dict(reconciliation_report or {})
        report_unresolved = report.get("unresolved_total")
        if unresolved_total is None:
            unresolved_total = computed_unresolved
        if unresolved_total < computed_unresolved:
            raise TerritoryCatalogValidationError(
                "unresolved total cannot be less than hidden nonselectable count"
            )
        if report and report_unresolved != unresolved_total:
            raise TerritoryCatalogValidationError(
                "reconciliation report unresolved total differs from supplied evidence"
            )
        report.setdefault("unresolved_total", unresolved_total)

        async with self._factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:provider, 0))"),
                    {"provider": provider},
                )
                version = (
                    await session.execute(
                        select(TerritoryCatalogVersion)
                        .where(
                            TerritoryCatalogVersion.provider == provider,
                            TerritoryCatalogVersion.catalog_version == catalog_version,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if version is not None:
                    raise TerritoryCatalogValidationError(
                        "catalogue versions are immutable and may be loaded only once"
                    )
                version = TerritoryCatalogVersion(
                    provider=provider,
                    catalog_version=catalog_version,
                    status="DRAFT",
                    source_revision=source_revision,
                    source_sha256=source_sha256,
                    record_count=len(prepared),
                    selectable_count=computed_selectable,
                    hidden_nonselectable_count=computed_unresolved,
                    unresolved_total=unresolved_total,
                    reconciliation_report=report,
                )
                session.add(version)
                await session.flush()

                session.add_all(
                    [
                        Territory(
                            provider=provider,
                            catalog_version=catalog_version,
                            region_id=row.region_id,
                            name=row.name,
                            kind=row.kind,
                            parent_region_id=row.parent_region_id,
                            external_code=row.external_code,
                            sort_key=row.sort_key,
                            is_selectable=row.is_selectable,
                        )
                        for row in prepared
                    ]
                )
        return len(prepared)

    async def activate(
        self,
        *,
        provider: str,
        catalog_version: str,
        expected_source_sha256: str,
        expected_record_count: int,
        expected_unresolved_total: int,
    ) -> None:
        """Atomically switch the read pointer after revalidating persisted rows."""

        provider = provider.strip()
        catalog_version = catalog_version.strip()
        async with self._factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:provider, 0))"),
                    {"provider": provider},
                )
                version = (
                    await session.execute(
                        select(TerritoryCatalogVersion)
                        .where(
                            TerritoryCatalogVersion.provider == provider,
                            TerritoryCatalogVersion.catalog_version == catalog_version,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if version is None or version.status != "DRAFT":
                    raise TerritoryCatalogValidationError("catalogue revision is not a draft")
                self._validate_activation_evidence(
                    version,
                    expected_source_sha256=expected_source_sha256,
                    expected_record_count=expected_record_count,
                    expected_unresolved_total=expected_unresolved_total,
                )
                poll_row = (
                    await session.execute(
                        select(AlertPollState)
                        .where(AlertPollState.singleton_id == 1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if poll_row is None:
                    raise TerritoryCatalogValidationError("alert poll state singleton is missing")
                stored = list(
                    (
                        await session.execute(
                            select(Territory).where(
                                Territory.provider == provider,
                                Territory.catalog_version == catalog_version,
                            )
                        )
                    ).scalars()
                )
                prepared = validate_territory_rows([self._draft(row) for row in stored])
                if not self._region_ids_match(prepared, self._expected_region_ids):
                    raise TerritoryCatalogValidationError(
                        "candidate territory UIDs differ from the official provider registry"
                    )
                if len(stored) != version.record_count:
                    raise TerritoryCatalogValidationError(
                        "catalogue record count differs from imported metadata"
                    )
                selectable_count = sum(row.is_selectable for row in prepared)
                if (
                    version.selectable_count != selectable_count
                    or version.hidden_nonselectable_count != len(prepared) - selectable_count
                ):
                    raise TerritoryCatalogValidationError(
                        "catalogue reconciliation counts differ from persisted rows"
                    )

                self._advance_alert_poll_fence(poll_row)

                scopes = list(
                    (
                        await session.execute(
                            select(AlertScope).order_by(AlertScope.id).with_for_update()
                        )
                    ).scalars()
                )
                plan = self._plan_scope_reconciliation(
                    scopes,
                    provider=provider,
                    catalog_version=catalog_version,
                    rows=stored,
                )
                scope_ids = [scope.id for scope, _applicable_ids in plan]
                snapshots: list[AlertSnapshot] = []
                if scope_ids:
                    snapshots = list(
                        (
                            await session.execute(
                                select(AlertSnapshot)
                                .where(AlertSnapshot.scope_id.in_(scope_ids))
                                .order_by(AlertSnapshot.scope_id)
                                .with_for_update()
                            )
                        ).scalars()
                    )
                    await session.execute(
                        delete(AlertEpisode).where(AlertEpisode.scope_id.in_(scope_ids))
                    )
                now = datetime.now(UTC)
                self._apply_scope_reconciliation(
                    plan,
                    provider=provider,
                    catalog_version=catalog_version,
                )
                for snapshot in snapshots:
                    self._reset_snapshot(snapshot, now=now)

                pointer = (
                    await session.execute(
                        select(ActiveTerritoryCatalog)
                        .where(ActiveTerritoryCatalog.provider == provider)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if pointer is not None:
                    previous = (
                        await session.execute(
                            select(TerritoryCatalogVersion)
                            .where(
                                TerritoryCatalogVersion.provider == provider,
                                TerritoryCatalogVersion.catalog_version == pointer.catalog_version,
                            )
                            .with_for_update()
                        )
                    ).scalar_one()
                    previous.status = "RETIRED"
                    # Satisfy the partial one-ACTIVE-per-provider index before
                    # marking the replacement active in the same transaction.
                    await session.flush()
                    pointer.catalog_version = catalog_version
                    pointer.activated_at = now
                else:
                    session.add(
                        ActiveTerritoryCatalog(
                            provider=provider,
                            catalog_version=catalog_version,
                            activated_at=now,
                        )
                    )
                version.status = "ACTIVE"
                version.activated_at = now

    def _resolve_expected_region_ids(
        self,
        requested: frozenset[str] | None,
    ) -> frozenset[str] | None:
        configured = self._expected_region_ids
        if configured is not None and requested is not None and configured != requested:
            raise TerritoryRuntimeNotReadyError(
                "configured territory UIDs differ from the requested provider registry"
            )
        return requested if requested is not None else configured

    @staticmethod
    def _region_ids_match(
        rows: Sequence[TerritoryDraftRow],
        expected_region_ids: frozenset[str] | None,
    ) -> bool:
        return (
            expected_region_ids is None
            or frozenset(row.region_id for row in rows) == expected_region_ids
        )

    def _assert_active_rows_expected(self, rows: Sequence[Territory]) -> None:
        try:
            prepared = validate_territory_rows(self._draft(row) for row in rows)
        except TerritoryCatalogValidationError as error:
            raise TerritoryRuntimeNotReadyError(
                "active territory catalogue hierarchy is invalid"
            ) from error
        if not self._region_ids_match(prepared, self._expected_region_ids):
            raise TerritoryRuntimeNotReadyError(
                "active territory catalogue UIDs differ from the official provider registry"
            )

    @staticmethod
    def _validate_activation_evidence(
        version: TerritoryCatalogVersion,
        *,
        expected_source_sha256: str,
        expected_record_count: int,
        expected_unresolved_total: int,
    ) -> None:
        report_total = version.reconciliation_report.get("unresolved_total")
        if (
            version.source_sha256 != expected_source_sha256.strip().lower()
            or version.record_count != expected_record_count
            or version.unresolved_total != expected_unresolved_total
            or report_total != expected_unresolved_total
        ):
            raise TerritoryCatalogValidationError(
                "locked draft does not match the reviewed activation evidence"
            )

    def _plan_scope_reconciliation(
        self,
        scopes: Sequence[AlertScope],
        *,
        provider: str,
        catalog_version: str,
        rows: Sequence[Territory],
    ) -> tuple[tuple[AlertScope, tuple[str, ...]], ...]:
        """Preflight every scope without mutation so one bad UID aborts all."""

        planned: list[tuple[AlertScope, tuple[str, ...]]] = []
        for scope in scopes:
            try:
                applicable_ids = self._scope_closure_from_rows(
                    rows,
                    catalog_version=catalog_version,
                    region_id=scope.selected_region_id,
                )
            except LookupError as error:
                raise TerritoryCatalogValidationError(
                    "candidate catalogue cannot reconcile existing selected UID "
                    f"{scope.selected_region_id!r}"
                ) from error
            planned.append((scope, applicable_ids))
        return tuple(planned)

    @staticmethod
    def _apply_scope_reconciliation(
        plan: Sequence[tuple[AlertScope, tuple[str, ...]]],
        *,
        provider: str,
        catalog_version: str,
    ) -> None:
        for scope, applicable_ids in plan:
            scope.territory_provider = provider
            scope.territory_catalog_version = catalog_version
            scope.applicable_region_ids = list(applicable_ids)

    @staticmethod
    def _advance_alert_poll_fence(row: AlertPollState) -> None:
        """Invalidate in-flight fetches without changing operator circuit policy."""

        row.fetch_revision += 1
        row.last_modified = None
        row.last_success_at = None
        # Circuit/backoff fields are operator/provider policy, not catalogue
        # freshness.  Preserve them so activation cannot reopen a 401/403
        # circuit or bypass a provider-request retry boundary.

    @staticmethod
    def _reset_snapshot(snapshot: AlertSnapshot, *, now: datetime) -> None:
        snapshot.state = "UNKNOWN"
        snapshot.last_fetch_revision = None
        snapshot.last_full_active_episodes = None
        snapshot.processed_update_ids = []
        snapshot.pending_clear_candidate = False
        snapshot.updated_at = now

    async def list_top(self, *, provider: str) -> tuple[TerritoryNode, ...]:
        return await self._list(provider=provider, parent_region_id=None)

    async def list_children(
        self, *, provider: str, parent_region_id: str
    ) -> tuple[TerritoryNode, ...]:
        return await self._list(provider=provider, parent_region_id=parent_region_id)

    async def get_path(self, *, provider: str, region_id: str) -> tuple[TerritoryNode, ...]:
        version, rows = await self._active_rows(provider)
        return self._path_from_rows(rows, catalog_version=version, region_id=region_id)

    def _path_from_rows(
        self,
        rows: Sequence[Territory],
        *,
        catalog_version: str,
        region_id: str,
    ) -> tuple[TerritoryNode, ...]:
        by_id = {row.region_id: row for row in rows}
        selected = by_id.get(region_id)
        if selected is None or not selected.is_selectable:
            raise LookupError(region_id)
        path: list[Territory] = []
        seen: set[str] = set()
        current: Territory | None = selected
        while current is not None:
            if current.region_id in seen:
                raise RuntimeError("active territory catalogue contains a cycle")
            seen.add(current.region_id)
            path.append(current)
            if current.parent_region_id is None:
                current = None
            else:
                parent = by_id.get(current.parent_region_id)
                if parent is None:
                    raise RuntimeError("active territory catalogue has an orphaned node")
                current = parent
        path.reverse()
        return tuple(self._node(row, catalog_version) for row in path)

    def _scope_closure_from_rows(
        self,
        rows: Sequence[Territory],
        *,
        catalog_version: str,
        region_id: str,
    ) -> tuple[str, ...]:
        """Return ancestors plus the selected node's complete provider subtree.

        The ancestor path makes a parent alert applicable to a selected child.
        The subtree makes a child alert applicable to a selected raion/oblast.
        Sibling branches are never traversed for a selected hromada or raion.
        """

        path = self._path_from_rows(
            rows,
            catalog_version=catalog_version,
            region_id=region_id,
        )
        applicable = [node.region_id for node in path]
        seen = set(applicable)
        depth = {"OBLAST": 0, "SPECIAL_STATUS_CITY": 0, "RAION": 1, "HROMADA": 2}
        children: dict[str, list[Territory]] = {}
        for row in rows:
            if row.parent_region_id is not None:
                children.setdefault(row.parent_region_id, []).append(row)
        for child_rows in children.values():
            child_rows.sort(
                key=lambda row: (
                    depth[row.kind],
                    row.sort_key,
                    row.name,
                    row.region_id,
                )
            )

        pending = list(reversed(children.get(region_id, [])))
        while pending:
            current = pending.pop()
            if current.region_id in seen:
                raise RuntimeError("active territory catalogue contains a cycle")
            seen.add(current.region_id)
            applicable.append(current.region_id)
            pending.extend(reversed(children.get(current.region_id, [])))
        return tuple(applicable)

    async def resolve_scope(self, *, provider: str, region_id: str) -> ResolvedTerritoryScope:
        version, rows = await self._active_rows(provider)
        return self._resolved_scope(
            provider=provider,
            catalog_version=version,
            region_id=region_id,
            rows=rows,
        )

    async def resolve_scope_in_session(
        self,
        session: AsyncSession,
        *,
        provider: str,
        region_id: str,
    ) -> ResolvedTerritoryScope:
        """Resolve and pin the active path under the activation serialization lock."""

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:provider, 0))"),
            {"provider": provider},
        )
        pointer = (
            await session.execute(
                select(ActiveTerritoryCatalog)
                .where(ActiveTerritoryCatalog.provider == provider)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if pointer is None:
            raise LookupError(f"no active territory catalogue for {provider!r}")
        version = (
            await session.execute(
                select(TerritoryCatalogVersion).where(
                    TerritoryCatalogVersion.provider == provider,
                    TerritoryCatalogVersion.catalog_version == pointer.catalog_version,
                )
            )
        ).scalar_one_or_none()
        if version is None or version.status != "ACTIVE":
            raise RuntimeError("active territory pointer targets a non-active revision")
        rows = list(
            (
                await session.execute(
                    select(Territory).where(
                        Territory.provider == provider,
                        Territory.catalog_version == pointer.catalog_version,
                    )
                )
            ).scalars()
        )
        if not rows:
            raise RuntimeError("active territory catalogue is empty")
        self._assert_active_rows_expected(rows)
        return self._resolved_scope(
            provider=provider,
            catalog_version=pointer.catalog_version,
            region_id=region_id,
            rows=rows,
        )

    def _resolved_scope(
        self,
        *,
        provider: str,
        catalog_version: str,
        region_id: str,
        rows: Sequence[Territory],
    ) -> ResolvedTerritoryScope:
        path = self._path_from_rows(rows, catalog_version=catalog_version, region_id=region_id)
        return ResolvedTerritoryScope(
            provider=provider,
            catalog_version=catalog_version,
            selected_region_id=region_id,
            applicable_region_ids=self._scope_closure_from_rows(
                rows,
                catalog_version=catalog_version,
                region_id=region_id,
            ),
            path=path,
        )

    async def scope_closure(self, *, provider: str, region_id: str) -> tuple[str, ...]:
        """Return ancestor UIDs plus the selected provider subtree."""

        resolved = await self.resolve_scope(provider=provider, region_id=region_id)
        return resolved.applicable_region_ids

    async def _list(
        self, *, provider: str, parent_region_id: str | None
    ) -> tuple[TerritoryNode, ...]:
        version, rows = await self._active_rows(provider)
        selected = [
            row for row in rows if row.parent_region_id == parent_region_id and row.is_selectable
        ]
        selected.sort(key=lambda row: (row.sort_key, row.name, row.region_id))
        return tuple(self._node(row, version) for row in selected)

    async def _active_rows(self, provider: str) -> tuple[str, Sequence[Territory]]:
        async with self._factory() as session:
            pointer = await session.get(ActiveTerritoryCatalog, provider)
            if pointer is None:
                raise LookupError(f"no active territory catalogue for {provider!r}")
            version = await session.get(
                TerritoryCatalogVersion, (provider, pointer.catalog_version)
            )
            if version is None or version.status != "ACTIVE":
                raise RuntimeError("active territory pointer targets a non-active revision")
            rows = list(
                (
                    await session.execute(
                        select(Territory).where(
                            Territory.provider == provider,
                            Territory.catalog_version == pointer.catalog_version,
                        )
                    )
                ).scalars()
            )
        if not rows:
            raise RuntimeError("active territory catalogue is empty")
        self._assert_active_rows_expected(rows)
        return pointer.catalog_version, rows

    @staticmethod
    def _draft(row: Territory) -> TerritoryDraftRow:
        return TerritoryDraftRow(
            region_id=row.region_id,
            name=row.name,
            kind=row.kind,
            parent_region_id=row.parent_region_id,
            external_code=row.external_code,
            sort_key=row.sort_key,
            is_selectable=row.is_selectable,
        )

    @staticmethod
    def _node(row: Territory, catalog_version: str) -> TerritoryNode:
        return TerritoryNode(
            region_id=row.region_id,
            name=row.name,
            kind=row.kind,
            parent_region_id=row.parent_region_id,
            external_code=row.external_code,
            catalog_version=catalog_version,
            is_selectable=row.is_selectable,
        )
