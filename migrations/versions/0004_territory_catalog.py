"""Add a versioned, database-served alerts.in.ua territory hierarchy.

Revision ID: 0004_territory_catalog
Revises: 0003_pending_import_payload

Existing alert scopes deliberately keep NULL catalogue metadata.  A separate,
reviewed backfill can pin legacy oblast-only scopes after the first catalogue is
activated; this migration never guesses that association.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0004_territory_catalog"
down_revision = "0003_pending_import_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 historically calls current Base.metadata.create_all().  On a
    # brand-new database that means these current-model objects already exist by
    # the time Alembic reaches 0004.  Existing installations have none of them.
    # Support those two complete states and fail closed on a partial schema.
    if context.is_offline_mode():
        # Revision 0001 renders current Base.metadata for a from-scratch offline
        # script, so this schema is already present in that generated SQL.
        return
    inspector = sa.inspect(op.get_bind())
    hierarchy_tables = {
        "territory_catalog_versions",
        "territories",
        "active_territory_catalogs",
    }
    present_tables = hierarchy_tables.intersection(inspector.get_table_names())
    alert_columns = {column["name"] for column in inspector.get_columns("alert_scopes")}
    new_alert_columns = {"territory_provider", "territory_catalog_version"}
    present_alert_columns = new_alert_columns.intersection(alert_columns)
    if present_tables == hierarchy_tables and present_alert_columns == new_alert_columns:
        return
    if present_tables or present_alert_columns:
        raise RuntimeError("partial territory catalogue schema requires operator review")

    op.create_table(
        "territory_catalog_versions",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("source_revision", sa.String(length=160), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("selectable_count", sa.Integer(), nullable=False),
        sa.Column("hidden_nonselectable_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_total", sa.Integer(), nullable=False),
        sa.Column(
            "reconciliation_report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','RETIRED')",
            name="ck_territory_catalog_versions_status_allowed",
        ),
        sa.CheckConstraint(
            "record_count > 0", name="ck_territory_catalog_versions_record_count_positive"
        ),
        sa.CheckConstraint(
            "selectable_count >= 0 AND selectable_count <= record_count",
            name="ck_territory_catalog_versions_selectable_count_range",
        ),
        sa.CheckConstraint(
            "hidden_nonselectable_count >= 0 AND hidden_nonselectable_count <= record_count",
            name="ck_territory_catalog_versions_hidden_nonselectable_count_range",
        ),
        sa.CheckConstraint(
            "selectable_count + hidden_nonselectable_count = record_count",
            name="ck_territory_catalog_versions_reconciliation_counts_complete",
        ),
        sa.CheckConstraint(
            "unresolved_total >= hidden_nonselectable_count",
            name="ck_territory_catalog_versions_unresolved_total_covers_hidden",
        ),
        sa.CheckConstraint(
            "char_length(source_sha256) = 64",
            name="ck_territory_catalog_versions_source_sha256_length",
        ),
        sa.PrimaryKeyConstraint(
            "provider", "catalog_version", name="pk_territory_catalog_versions"
        ),
    )
    op.create_index(
        "uq_territory_catalog_versions_one_active",
        "territory_catalog_versions",
        ["provider"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "territories",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=96), nullable=False),
        sa.Column("region_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("parent_region_id", sa.String(length=64), nullable=True),
        sa.Column("external_code", sa.String(length=64), nullable=True),
        sa.Column("sort_key", sa.String(length=240), nullable=False),
        sa.Column("is_selectable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('OBLAST','SPECIAL_STATUS_CITY','RAION','HROMADA')",
            name="ck_territories_kind_allowed",
        ),
        sa.CheckConstraint("char_length(btrim(name)) > 0", name="ck_territories_name_nonempty"),
        sa.ForeignKeyConstraint(
            ["provider", "catalog_version"],
            [
                "territory_catalog_versions.provider",
                "territory_catalog_versions.catalog_version",
            ],
            name="fk_territories_catalog",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider", "catalog_version", "parent_region_id"],
            ["territories.provider", "territories.catalog_version", "territories.region_id"],
            name="fk_territories_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("provider", "catalog_version", "region_id", name="pk_territories"),
    )
    op.create_index(
        "ix_territories_parent",
        "territories",
        ["provider", "catalog_version", "parent_region_id"],
        unique=False,
    )
    op.create_table(
        "active_territory_catalogs",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=96), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["provider", "catalog_version"],
            [
                "territory_catalog_versions.provider",
                "territory_catalog_versions.catalog_version",
            ],
            name="fk_active_territory_catalogs_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("provider", name="pk_active_territory_catalogs"),
    )
    op.add_column("alert_scopes", sa.Column("territory_provider", sa.String(length=64)))
    op.add_column("alert_scopes", sa.Column("territory_catalog_version", sa.String(length=96)))
    op.create_check_constraint(
        op.f("ck_alert_scopes_territory_revision_pair"),
        "alert_scopes",
        "(territory_provider IS NULL) = (territory_catalog_version IS NULL)",
    )
    op.create_foreign_key(
        "fk_alert_scopes_selected_territory",
        "alert_scopes",
        "territories",
        ["territory_provider", "territory_catalog_version", "selected_region_id"],
        ["provider", "catalog_version", "region_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    if context.is_offline_mode():
        return
    op.drop_constraint("fk_alert_scopes_selected_territory", "alert_scopes", type_="foreignkey")
    op.drop_constraint(
        op.f("ck_alert_scopes_territory_revision_pair"),
        "alert_scopes",
        type_="check",
    )
    op.drop_column("alert_scopes", "territory_catalog_version")
    op.drop_column("alert_scopes", "territory_provider")
    op.drop_table("active_territory_catalogs")
    op.drop_index("ix_territories_parent", table_name="territories")
    op.drop_table("territories")
    op.drop_index(
        "uq_territory_catalog_versions_one_active", table_name="territory_catalog_versions"
    )
    op.drop_table("territory_catalog_versions")
