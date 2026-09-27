from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from inspect import getsource, signature
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from time_tl.bot.models import NotificationMode
from time_tl.infrastructure.db.models import (
    ActiveTerritoryCatalog,
    AlertPollState,
    AlertScope,
    AlertSnapshot,
    Territory,
    TerritoryCatalogVersion,
    User,
)
from time_tl.infrastructure.db.territories import (
    PostgresTerritoryCatalogRepository,
    TerritoryCatalogValidationError,
    TerritoryDraftRow,
    TerritoryRuntimeNotReadyError,
    validate_territory_rows,
)
from time_tl.runtime.services import PostgresRegionConfirmationService


def _valid_rows() -> list[TerritoryDraftRow]:
    return [
        TerritoryDraftRow("9", "Дніпропетровська область", "OBLAST", None, "UA12000000000090473"),
        TerritoryDraftRow("90", "Кам'янський район", "RAION", "9", "UA12040000000020867"),
        TerritoryDraftRow("901", "Кам'янська громада", "HROMADA", "90", "UA12040130000057887"),
    ]


def test_validates_and_orders_oblast_raion_hromada_tree() -> None:
    rows = validate_territory_rows(reversed(_valid_rows()))

    assert [row.region_id for row in rows] == ["9", "90", "901"]
    assert rows[2].sort_key == "кам'янська громада"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (_valid_rows() + [_valid_rows()[0]], "duplicate territory UID"),
        (
            [TerritoryDraftRow("90", "Район", "RAION", "missing")],
            "references missing parent",
        ),
        (
            [
                TerritoryDraftRow("90", "Район", "RAION", "901"),
                TerritoryDraftRow("901", "Громада", "HROMADA", "90"),
            ],
            "contains a cycle",
        ),
        (
            [
                TerritoryDraftRow("9", "Область", "OBLAST", None),
                TerritoryDraftRow("901", "Громада", "HROMADA", "9"),
            ],
            "requires parent kind RAION",
        ),
        (
            [
                TerritoryDraftRow("9", "Область", "OBLAST", None, "UA1"),
                TerritoryDraftRow("90", "Район", "RAION", "9", "UA2"),
            ],
            "has no hromada",
        ),
    ],
)
def test_rejects_unsafe_catalogues(rows: list[TerritoryDraftRow], message: str) -> None:
    with pytest.raises(TerritoryCatalogValidationError, match=message):
        validate_territory_rows(rows)


def test_alert_scope_catalogue_reference_is_nullable_for_legacy_rows() -> None:
    table = AlertScope.__table__

    assert table.c.territory_provider.nullable
    assert table.c.territory_catalog_version.nullable
    assert table.c.selected_region_id.nullable is False


def test_special_status_city_may_be_a_leaf() -> None:
    rows = validate_territory_rows(
        [TerritoryDraftRow("31", "м. Київ", "SPECIAL_STATUS_CITY", None, "UA80000000000093317")]
    )

    assert [(row.region_id, row.kind) for row in rows] == [("31", "SPECIAL_STATUS_CITY")]


def test_oblast_without_provider_children_may_be_selected_at_oblast_level() -> None:
    rows = validate_territory_rows(
        [
            TerritoryDraftRow(
                "29", "Автономна Республіка Крим", "OBLAST", None, "UA01000000000013043"
            )
        ]
    )

    assert [(row.region_id, row.kind) for row in rows] == [("29", "OBLAST")]


def test_selectable_territory_requires_external_code_and_resolved_ancestors() -> None:
    with pytest.raises(TerritoryCatalogValidationError, match="has no external code"):
        validate_territory_rows([TerritoryDraftRow("31", "м. Київ", "SPECIAL_STATUS_CITY", None)])

    with pytest.raises(TerritoryCatalogValidationError, match="has unresolved ancestor"):
        validate_territory_rows(
            [
                TerritoryDraftRow("9", "Область", "OBLAST", None, is_selectable=False),
                TerritoryDraftRow("90", "Район", "RAION", "9", is_selectable=False),
                TerritoryDraftRow("901", "Громада", "HROMADA", "90", "UA3"),
            ]
        )


def test_unresolved_nonselectable_leaf_does_not_require_external_code() -> None:
    rows = validate_territory_rows(
        [TerritoryDraftRow("31", "м. Київ", "SPECIAL_STATUS_CITY", None, is_selectable=False)]
    )

    assert rows[0].external_code is None
    assert rows[0].is_selectable is False


def test_catalogue_version_persists_reconciliation_metadata_contract() -> None:
    table = TerritoryCatalogVersion.__table__
    parameters = signature(PostgresTerritoryCatalogRepository.load_draft).parameters

    assert table.c.selectable_count.nullable is False
    assert table.c.hidden_nonselectable_count.nullable is False
    assert table.c.unresolved_total.nullable is False
    assert table.c.reconciliation_report.nullable is False
    assert parameters["selectable_count"].default is None
    assert parameters["hidden_nonselectable_count"].default is None
    assert parameters["unresolved_total"].default is None
    assert parameters["reconciliation_report"].default is None


class ReadModelRepository(PostgresTerritoryCatalogRepository):
    def __init__(self, rows: Sequence[Territory]) -> None:
        self.rows = rows
        # The read methods are overridden below and never access this factory.
        super().__init__(None)  # type: ignore[arg-type]

    async def _active_rows(self, provider: str) -> tuple[str, Sequence[Territory]]:
        assert provider == "alerts.in.ua"
        return "2026-09-24", self.rows


def _stored_rows() -> list[Territory]:
    source = _valid_rows() + [
        TerritoryDraftRow("902", "Верхньодніпровська громада", "HROMADA", "90", "UA4"),
        TerritoryDraftRow("91", "Дніпровський район", "RAION", "9", "UA5"),
        TerritoryDraftRow("911", "Дніпровська громада", "HROMADA", "91", "UA6"),
        TerritoryDraftRow("31", "м. Київ", "SPECIAL_STATUS_CITY", None, "UA7"),
    ]
    return [
        Territory(
            provider="alerts.in.ua",
            catalog_version="2026-09-24",
            region_id=row.region_id,
            name=row.name,
            kind=row.kind,
            parent_region_id=row.parent_region_id,
            external_code=row.external_code,
            sort_key=row.name.casefold(),
            is_selectable=True,
        )
        for row in source
    ]


@pytest.mark.asyncio
async def test_active_read_model_serves_tree_path_and_alert_scope_closure() -> None:
    repository = ReadModelRepository(_stored_rows())

    top = await repository.list_top(provider="alerts.in.ua")
    children = await repository.list_children(provider="alerts.in.ua", parent_region_id="9")
    path = await repository.get_path(provider="alerts.in.ua", region_id="901")
    closure = await repository.scope_closure(provider="alerts.in.ua", region_id="901")
    resolved = await repository.resolve_scope(provider="alerts.in.ua", region_id="901")

    assert [node.region_id for node in top] == ["9", "31"]
    assert [node.region_id for node in children] == ["91", "90"]
    assert [node.name for node in path] == [
        "Дніпропетровська область",
        "Кам'янський район",
        "Кам'янська громада",
    ]
    assert closure == ("9", "90", "901")
    assert resolved.catalog_version == "2026-09-24"
    assert resolved.selected_region_id == "901"
    assert resolved.applicable_region_ids == closure


@pytest.mark.asyncio
async def test_scope_closure_preserves_all_selection_levels() -> None:
    repository = ReadModelRepository(_stored_rows())

    assert await repository.scope_closure(provider="alerts.in.ua", region_id="901") == (
        "9",
        "90",
        "901",
    )
    assert await repository.scope_closure(provider="alerts.in.ua", region_id="90") == ("9", "90")
    assert await repository.scope_closure(provider="alerts.in.ua", region_id="9") == ("9",)
    assert await repository.scope_closure(provider="alerts.in.ua", region_id="31") == ("31",)


def _scope(selected_region_id: str, *, legacy: bool = False) -> AlertScope:
    return AlertScope(
        id=uuid4(),
        user_id=uuid4(),
        selected_region_id=selected_region_id,
        applicable_region_ids=["old"],
        territory_provider=None if legacy else "alerts.in.ua",
        territory_catalog_version=None if legacy else "old-version",
    )


def test_activation_plan_reconciles_legacy_scope_with_selected_and_ancestors_only() -> None:
    repository = ReadModelRepository(_stored_rows())
    scope = _scope("901", legacy=True)
    original_id = scope.id
    original_user_id = scope.user_id

    plan = repository._plan_scope_reconciliation(
        [scope],
        provider="alerts.in.ua",
        catalog_version="2026-09-24",
        rows=_stored_rows(),
    )
    repository._apply_scope_reconciliation(
        plan,
        provider="alerts.in.ua",
        catalog_version="2026-09-24",
    )

    assert scope.id == original_id
    assert scope.user_id == original_user_id
    assert scope.selected_region_id == "901"
    assert scope.territory_provider == "alerts.in.ua"
    assert scope.territory_catalog_version == "2026-09-24"
    assert scope.applicable_region_ids == ["9", "90", "901"]


def test_activation_reconciliation_uses_path_not_descendant_closure() -> None:
    repository = ReadModelRepository(_stored_rows())
    oblast_scope = _scope("9")
    raion_scope = _scope("90")

    plan = repository._plan_scope_reconciliation(
        [oblast_scope, raion_scope],
        provider="alerts.in.ua",
        catalog_version="2026-09-24",
        rows=_stored_rows(),
    )
    repository._apply_scope_reconciliation(
        plan,
        provider="alerts.in.ua",
        catalog_version="2026-09-24",
    )

    assert oblast_scope.applicable_region_ids == ["9"]
    assert raion_scope.applicable_region_ids == ["9", "90"]


def test_activation_preflight_aborts_without_mutating_any_scope() -> None:
    repository = ReadModelRepository(_stored_rows())
    valid = _scope("901", legacy=True)
    missing = _scope("missing", legacy=True)

    with pytest.raises(TerritoryCatalogValidationError, match="cannot reconcile"):
        repository._plan_scope_reconciliation(
            [valid, missing],
            provider="alerts.in.ua",
            catalog_version="2026-09-24",
            rows=_stored_rows(),
        )

    assert valid.territory_provider is None
    assert valid.territory_catalog_version is None
    assert valid.applicable_region_ids == ["old"]
    assert missing.territory_provider is None
    assert missing.applicable_region_ids == ["old"]


def test_activation_preflight_rejects_unselectable_existing_uid() -> None:
    repository = ReadModelRepository(_stored_rows())
    rows = _stored_rows()
    selected = next(row for row in rows if row.region_id == "901")
    selected.is_selectable = False

    with pytest.raises(TerritoryCatalogValidationError, match="cannot reconcile"):
        repository._plan_scope_reconciliation(
            [_scope("901")],
            provider="alerts.in.ua",
            catalog_version="2026-09-24",
            rows=rows,
        )


def test_activation_snapshot_reset_is_exact_and_preserves_identity() -> None:
    repository = ReadModelRepository(_stored_rows())
    scope_id = uuid4()
    old_updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    reset_at = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    snapshot = AlertSnapshot(
        scope_id=scope_id,
        state="ACTIVE",
        last_fetch_revision=42,
        last_full_active_episodes=[{"episode_id": "old"}],
        processed_update_ids=["old-update"],
        pending_clear_candidate=True,
        updated_at=old_updated_at,
    )

    repository._reset_snapshot(snapshot, now=reset_at)

    assert snapshot.scope_id == scope_id
    assert snapshot.state == "UNKNOWN"
    assert snapshot.last_fetch_revision is None
    assert snapshot.last_full_active_episodes is None
    assert snapshot.processed_update_ids == []
    assert snapshot.pending_clear_candidate is False
    assert snapshot.updated_at == reset_at


def test_activation_advances_poll_fence_and_preserves_operator_circuit_policy() -> None:
    retry_at = datetime(2026, 9, 24, 12, 5, tzinfo=UTC)
    success_at = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    poll = AlertPollState(
        singleton_id=1,
        fetch_revision=41,
        last_modified="old-marker",
        last_success_at=success_at,
        circuit_open=True,
        consecutive_failures=3,
        retry_not_before=retry_at,
    )

    PostgresTerritoryCatalogRepository._advance_alert_poll_fence(poll)

    assert poll.fetch_revision == 42
    assert poll.last_modified is None
    assert poll.last_success_at is None
    assert poll.circuit_open is True
    assert poll.consecutive_failures == 3
    assert poll.retry_not_before == retry_at


def test_activation_lock_order_is_poll_row_before_alert_scopes() -> None:
    source = getsource(PostgresTerritoryCatalogRepository.activate)

    assert source.index("select(AlertPollState)") < source.index("select(AlertScope)")


def test_model_parent_fk_is_composite_and_catalogue_scoped() -> None:
    parent_fk = next(
        constraint
        for constraint in Territory.__table__.foreign_key_constraints
        if constraint.name == "fk_territories_parent"
    )

    assert [element.parent.name for element in parent_fk.elements] == [
        "provider",
        "catalog_version",
        "parent_region_id",
    ]


class QueueResult:
    def __init__(self, *, scalar: Any = None, rows: Sequence[Any] = ()) -> None:
        self.scalar = scalar
        self.rows = rows

    def scalar_one_or_none(self) -> Any:
        return self.scalar

    def scalar_one(self) -> Any:
        return self.scalar

    def scalars(self) -> Sequence[Any]:
        return self.rows

    def one(self) -> tuple[Any, ...]:
        return tuple(self.rows)


class TransactionContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: Any) -> None:
        return None


class ConfirmationSession:
    def __init__(self, results: Sequence[QueueResult], snapshot: AlertSnapshot) -> None:
        self.results = list(results)
        self.snapshot = snapshot
        self.statements: list[str] = []

    async def __aenter__(self) -> ConfirmationSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def begin(self) -> TransactionContext:
        return TransactionContext()

    async def execute(self, statement: Any, _parameters: Any = None) -> QueueResult:
        self.statements.append(str(statement))
        return self.results.pop(0)

    async def get(self, model: type[Any], identity: Any) -> Any:
        assert model is AlertSnapshot
        assert identity == self.snapshot.scope_id
        return self.snapshot


class ConfirmationFactory:
    def __init__(self, session: ConfirmationSession) -> None:
        self.session = session

    def __call__(self) -> ConfirmationSession:
        return self.session


@pytest.mark.asyncio
async def test_confirmation_locks_provider_before_same_session_resolve_and_scope_write() -> None:
    provider = "alerts.in.ua"
    catalog_version = "2026-09-24"
    user_id = uuid4()
    scope_id = uuid4()
    user = User(id=user_id, telegram_user_id=777)
    scope = AlertScope(
        id=scope_id,
        user_id=user_id,
        selected_region_id="old",
        applicable_region_ids=["old"],
        territory_provider=None,
        territory_catalog_version=None,
    )
    snapshot = AlertSnapshot(
        scope_id=scope_id,
        state="ACTIVE",
        last_fetch_revision=5,
        last_full_active_episodes=[{"episode_id": "old"}],
        processed_update_ids=["old"],
        pending_clear_candidate=True,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    pointer = ActiveTerritoryCatalog(provider=provider, catalog_version=catalog_version)
    version = TerritoryCatalogVersion(
        provider=provider,
        catalog_version=catalog_version,
        status="ACTIVE",
        source_revision="reviewed",
        source_sha256="a" * 64,
        record_count=3,
        selectable_count=3,
        hidden_nonselectable_count=0,
        unresolved_total=0,
        reconciliation_report={"unresolved_total": 0},
    )
    session = ConfirmationSession(
        [
            QueueResult(),
            QueueResult(scalar=pointer),
            QueueResult(scalar=version),
            QueueResult(rows=_stored_rows()[:3]),
            QueueResult(scalar=user),
            QueueResult(scalar=scope),
            QueueResult(),
        ],
        snapshot,
    )
    catalog = PostgresTerritoryCatalogRepository(None)  # type: ignore[arg-type]
    service = PostgresRegionConfirmationService(ConfirmationFactory(session), catalog)
    account = SimpleNamespace(
        user_id=777,
        region_id="901",
        region_name="stale name",
        mode=NotificationMode.LESSONS_AND_SIRENS,
    )

    await service.confirm_region(account)

    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "active_territory_catalogs" in session.statements[1]
    assert "FOR UPDATE" in session.statements[1]
    assert "territory_catalog_versions" in session.statements[2]
    assert "FROM territories" in session.statements[3]
    assert "FROM users" in session.statements[4]
    assert "FROM alert_scopes" in session.statements[5]
    assert "DELETE FROM alert_episodes" in session.statements[6]
    assert session.results == []
    assert scope.selected_region_id == "901"
    assert scope.territory_catalog_version == catalog_version
    assert scope.applicable_region_ids == ["9", "90", "901"]
    assert snapshot.state == "UNKNOWN"
    assert account.region_name == "Кам'янська громада"


def _catalog_version(*, status: str = "DRAFT") -> TerritoryCatalogVersion:
    return TerritoryCatalogVersion(
        provider="alerts.in.ua",
        catalog_version="reviewed-v1",
        status=status,
        source_revision="reviewed",
        source_sha256="a" * 64,
        record_count=3,
        selectable_count=3,
        hidden_nonselectable_count=0,
        unresolved_total=295,
        reconciliation_report={"unresolved_total": 295},
    )


@pytest.mark.parametrize(
    ("source_sha256", "record_count", "unresolved_total"),
    [
        ("b" * 64, 3, 295),
        ("a" * 64, 2, 295),
        ("a" * 64, 3, 294),
    ],
)
def test_activation_rejects_any_reviewed_evidence_mismatch(
    source_sha256: str, record_count: int, unresolved_total: int
) -> None:
    with pytest.raises(TerritoryCatalogValidationError, match="reviewed activation evidence"):
        PostgresTerritoryCatalogRepository._validate_activation_evidence(
            _catalog_version(),
            expected_source_sha256=source_sha256,
            expected_record_count=record_count,
            expected_unresolved_total=unresolved_total,
        )


@pytest.mark.asyncio
async def test_activation_mismatch_aborts_before_scope_read_or_reset() -> None:
    snapshot = AlertSnapshot(scope_id=uuid4(), state="ACTIVE")
    session = ConfirmationSession([QueueResult(), QueueResult(scalar=_catalog_version())], snapshot)
    repository = PostgresTerritoryCatalogRepository(ConfirmationFactory(session))

    with pytest.raises(TerritoryCatalogValidationError, match="reviewed activation evidence"):
        await repository.activate(
            provider="alerts.in.ua",
            catalog_version="reviewed-v1",
            expected_source_sha256="b" * 64,
            expected_record_count=3,
            expected_unresolved_total=295,
        )

    assert len(session.statements) == 2
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "territory_catalog_versions" in session.statements[1]
    assert all("alert_scopes" not in statement for statement in session.statements)
    assert snapshot.state == "ACTIVE"


@pytest.mark.asyncio
async def test_existing_draft_is_immutable_against_concurrent_substitution() -> None:
    snapshot = AlertSnapshot(scope_id=uuid4(), state="UNKNOWN")
    session = ConfirmationSession([QueueResult(), QueueResult(scalar=_catalog_version())], snapshot)
    repository = PostgresTerritoryCatalogRepository(ConfirmationFactory(session))

    with pytest.raises(TerritoryCatalogValidationError, match="loaded only once"):
        await repository.load_draft(
            provider="alerts.in.ua",
            catalog_version="reviewed-v1",
            source_revision="substitution-attempt",
            source_sha256="b" * 64,
            rows=_valid_rows(),
            selectable_count=3,
            hidden_nonselectable_count=0,
            unresolved_total=295,
            reconciliation_report={"unresolved_total": 295},
        )

    assert len(session.statements) == 2
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "territory_catalog_versions" in session.statements[1]
    assert all("DELETE" not in statement for statement in session.statements)


def _readiness_repository(
    results: Sequence[QueueResult],
) -> tuple[PostgresTerritoryCatalogRepository, ConfirmationSession]:
    snapshot = AlertSnapshot(scope_id=uuid4(), state="UNKNOWN")
    session = ConfirmationSession(results, snapshot)
    repository = PostgresTerritoryCatalogRepository(ConfirmationFactory(session))
    return repository, session


@pytest.mark.asyncio
async def test_runtime_readiness_rejects_missing_active_pointer() -> None:
    repository, session = _readiness_repository([QueueResult(), QueueResult(scalar=None)])

    with pytest.raises(TerritoryRuntimeNotReadyError, match="catalogue is missing"):
        await repository.assert_runtime_ready(provider="alerts.in.ua")

    assert len(session.statements) == 2
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "active_territory_catalogs" in session.statements[1]
    assert "FOR UPDATE" in session.statements[1]


@pytest.mark.parametrize("version", [None, _catalog_version(status="DRAFT")])
@pytest.mark.asyncio
async def test_runtime_readiness_rejects_missing_or_non_active_version(
    version: TerritoryCatalogVersion | None,
) -> None:
    pointer = ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="reviewed-v1")
    repository, _session = _readiness_repository(
        [QueueResult(), QueueResult(scalar=pointer), QueueResult(scalar=version)]
    )

    with pytest.raises(TerritoryRuntimeNotReadyError, match="non-active revision"):
        await repository.assert_runtime_ready(provider="alerts.in.ua")


@pytest.mark.parametrize("case", ["empty", "row_count", "selectable_count"])
@pytest.mark.asyncio
async def test_runtime_readiness_rejects_empty_or_count_mismatched_catalogue(
    case: str,
) -> None:
    pointer = ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="reviewed-v1")
    rows = _stored_rows()[:3]
    if case == "empty":
        rows = []
    elif case == "row_count":
        rows = [
            Territory(
                provider="alerts.in.ua",
                catalog_version="reviewed-v1",
                region_id="31",
                name="м. Київ",
                kind="SPECIAL_STATUS_CITY",
                parent_region_id=None,
                external_code="UA7",
                sort_key="київ",
                is_selectable=True,
            )
        ]
    else:
        rows[-1].is_selectable = False
    repository, _session = _readiness_repository(
        [
            QueueResult(),
            QueueResult(scalar=pointer),
            QueueResult(scalar=_catalog_version(status="ACTIVE")),
            QueueResult(rows=rows),
        ]
    )

    with pytest.raises(TerritoryRuntimeNotReadyError, match="catalogue"):
        await repository.assert_runtime_ready(provider="alerts.in.ua")


@pytest.mark.asyncio
async def test_runtime_readiness_rejects_legacy_or_mismatched_scope_bindings() -> None:
    pointer = ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="reviewed-v1")
    repository, session = _readiness_repository(
        [
            QueueResult(),
            QueueResult(scalar=pointer),
            QueueResult(scalar=_catalog_version(status="ACTIVE")),
            QueueResult(rows=_stored_rows()[:3]),
            QueueResult(rows=[_scope("901", legacy=True)]),
        ]
    )

    with pytest.raises(TerritoryRuntimeNotReadyError, match="not fully bound"):
        await repository.assert_runtime_ready(provider="alerts.in.ua")

    assert "FROM alert_scopes" in session.statements[4]
    assert "FOR UPDATE" in session.statements[4]


@pytest.mark.parametrize(
    ("selected_region_id", "applicable_ids", "message"),
    [
        ("missing", ["missing"], "missing or nonselectable"),
        ("901", ["9"], "closure differs"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_readiness_rejects_selected_uid_or_canonical_closure_mismatch(
    selected_region_id: str,
    applicable_ids: list[str],
    message: str,
) -> None:
    pointer = ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="reviewed-v1")
    scope = _scope(selected_region_id)
    scope.territory_catalog_version = "reviewed-v1"
    scope.applicable_region_ids = applicable_ids
    repository, _session = _readiness_repository(
        [
            QueueResult(),
            QueueResult(scalar=pointer),
            QueueResult(scalar=_catalog_version(status="ACTIVE")),
            QueueResult(rows=_stored_rows()[:3]),
            QueueResult(rows=[scope]),
        ]
    )

    with pytest.raises(TerritoryRuntimeNotReadyError, match=message):
        await repository.assert_runtime_ready(provider="alerts.in.ua")


@pytest.mark.asyncio
async def test_runtime_readiness_ready_state_checks_lock_pointer_counts_then_scopes() -> None:
    pointer = ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="reviewed-v1")
    scope = _scope("901")
    scope.territory_catalog_version = "reviewed-v1"
    scope.applicable_region_ids = ["9", "90", "901"]
    repository, session = _readiness_repository(
        [
            QueueResult(),
            QueueResult(scalar=pointer),
            QueueResult(scalar=_catalog_version(status="ACTIVE")),
            QueueResult(rows=_stored_rows()[:3]),
            QueueResult(rows=[scope]),
        ]
    )

    active = await repository.assert_runtime_ready(provider="alerts.in.ua")

    assert active == "reviewed-v1"
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "active_territory_catalogs" in session.statements[1]
    assert "territory_catalog_versions" in session.statements[2]
    assert "FROM territories" in session.statements[3]
    assert "FOR UPDATE" in session.statements[3]
    assert "FROM alert_scopes" in session.statements[4]
    assert "FOR UPDATE" in session.statements[4]
