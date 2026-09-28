"""Add personal manual-homework persistence.

Revision ID: 0008_homework_foundation
Revises: 0007_outbox_indeterminate

The migration is additive and does not touch alert, outbox, schedule-worker or
sender-worker state.  Historical revision 0001 creates current metadata on a
fresh database, so this revision is an online no-op when all four tables are
already present and fails closed if only part of the schema exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0008_homework_foundation"
down_revision = "0007_outbox_indeterminate"
branch_labels = None
depends_on = None

_TABLES = {
    "schedule_subjects",
    "homework_assignments",
    "homework_user_states",
    "pending_homework_drafts",
}


def upgrade() -> None:
    if context.is_offline_mode():
        # A targeted ``0007:0008 --sql`` migration cannot inspect whether the
        # historical 0001 metadata bootstrap already created these tables.
        # Returning here would emit only an alembic_version update and could
        # falsely attest a database with no homework schema. Require an online,
        # inspectable migration instead.
        raise RuntimeError("0008 homework migration requires online schema inspection")
    inspector = sa.inspect(op.get_bind())
    present = _TABLES.intersection(inspector.get_table_names())
    if present == _TABLES:
        return
    if present:
        raise RuntimeError("partial homework schema requires operator review")

    op.create_table(
        "schedule_subjects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) > 0", name="ck_schedule_subjects_name_nonempty"
        ),
        sa.CheckConstraint(
            "char_length(normalized_name) > 0",
            name="ck_schedule_subjects_normalized_name_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["schedule_profiles.id"],
            name="fk_schedule_subjects_profile_id_schedule_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_subjects"),
        sa.UniqueConstraint(
            "profile_id", "normalized_name", name="uq_schedule_subjects_profile_id"
        ),
        sa.UniqueConstraint("id", "profile_id", name="schedule_subject_identity"),
    )
    op.create_index(
        "ix_schedule_subjects_profile_id",
        "schedule_subjects",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "homework_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), server_default="MANUAL", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(text)) BETWEEN 1 AND 2000",
            name="ck_homework_assignments_text_length",
        ),
        sa.CheckConstraint(
            "source_type = 'MANUAL'",
            name="ck_homework_assignments_source_type_allowed",
        ),
        sa.CheckConstraint("version >= 1", name="ck_homework_assignments_version_positive"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_homework_assignments_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id", "profile_id"],
            ["schedule_subjects.id", "schedule_subjects.profile_id"],
            name="fk_homework_subject_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_homework_assignments"),
    )
    op.create_index(
        "ix_homework_assignments_owner_user_id",
        "homework_assignments",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_homework_assignments_owner_due",
        "homework_assignments",
        ["owner_user_id", "due_at", "id"],
        unique=False,
    )

    op.create_table(
        "homework_user_states",
        sa.Column("homework_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(completed AND completed_at IS NOT NULL) OR (NOT completed AND completed_at IS NULL)",
            name="ck_homework_user_states_completion_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["homework_id"],
            ["homework_assignments.id"],
            name="fk_homework_user_states_homework_id_homework_assignments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_homework_user_states_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("homework_id", "user_id", name="pk_homework_user_states"),
    )
    op.create_index(
        "ix_homework_user_states_user",
        "homework_user_states",
        ["user_id", "homework_id"],
        unique=False,
    )

    op.create_table(
        "pending_homework_drafts",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), server_default="MANUAL", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_homework_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(text)) BETWEEN 1 AND 2000",
            name="ck_pending_homework_drafts_text_length",
        ),
        sa.CheckConstraint(
            "source_type = 'MANUAL'",
            name="ck_pending_homework_drafts_source_type_allowed",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR confirmed_homework_id IS NOT NULL",
            name="ck_pending_homework_drafts_consumed_has_result",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_homework_id"],
            ["homework_assignments.id"],
            name="fk_pending_homework_confirmed_assignment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id", "profile_id"],
            ["schedule_subjects.id", "schedule_subjects.profile_id"],
            name="fk_pending_homework_subject_profile",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_pending_homework_drafts_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name="pk_pending_homework_drafts"),
        sa.UniqueConstraint(
            "confirmed_homework_id",
            name="uq_pending_homework_drafts_confirmed_homework_id",
        ),
    )
    op.create_index(
        "ix_pending_homework_drafts_user_id",
        "pending_homework_drafts",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_pending_homework_active_user_created",
        "pending_homework_drafts",
        ["user_id", "created_at", "token_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL AND consumed_at IS NULL"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("0008 homework migration requires online schema inspection")
    inspector = sa.inspect(op.get_bind())
    present = _TABLES.intersection(inspector.get_table_names())
    if not present:
        return
    if present != _TABLES:
        raise RuntimeError("partial homework schema requires operator review")
    op.drop_index("ix_pending_homework_active_user_created", table_name="pending_homework_drafts")
    op.drop_index("ix_pending_homework_drafts_user_id", table_name="pending_homework_drafts")
    op.drop_table("pending_homework_drafts")
    op.drop_index("ix_homework_user_states_user", table_name="homework_user_states")
    op.drop_table("homework_user_states")
    op.drop_index("ix_homework_assignments_owner_due", table_name="homework_assignments")
    op.drop_index("ix_homework_assignments_owner_user_id", table_name="homework_assignments")
    op.drop_table("homework_assignments")
    op.drop_index("ix_schedule_subjects_profile_id", table_name="schedule_subjects")
    op.drop_table("schedule_subjects")
