"""Persist fail-closed indeterminate outbox delivery outcomes.

Revision ID: 0007_outbox_indeterminate
Revises: 0006_outbox_terminal_retention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0007_outbox_indeterminate"
down_revision = "0006_outbox_terminal_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh databases are created from current metadata by 0001. Offline SQL
    # therefore needs no duplicate ALTER statements, matching earlier additive
    # migrations in this repository.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("outbox_messages")}
    if "dispatch_started_at" not in columns:
        op.add_column(
            "outbox_messages",
            sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "indeterminate_at" not in columns:
        op.add_column(
            "outbox_messages",
            sa.Column("indeterminate_at", sa.DateTime(timezone=True), nullable=True),
        )

    inspector = sa.inspect(bind)
    checks = {item["name"] for item in inspector.get_check_constraints("outbox_messages")}
    if not {"indeterminate_terminal", "ck_outbox_messages_indeterminate_terminal"} & checks:
        op.create_check_constraint(
            "indeterminate_terminal",
            "outbox_messages",
            "indeterminate_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dead_at IS NOT NULL "
            "AND delivered_at IS NULL)",
        )
    indexes = {item["name"] for item in inspector.get_indexes("outbox_messages")}
    if "ix_outbox_indeterminate" not in indexes:
        op.create_index(
            "ix_outbox_indeterminate",
            "outbox_messages",
            ["indeterminate_at", "id"],
            unique=False,
            postgresql_where=sa.text(
                "indeterminate_at IS NOT NULL AND delivered_at IS NULL"
            ),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("outbox_messages")}
    if "ix_outbox_indeterminate" in indexes:
        op.drop_index("ix_outbox_indeterminate", table_name="outbox_messages")
    checks = {item["name"] for item in inspector.get_check_constraints("outbox_messages")}
    terminal_check = next(
        (
            name
            for name in (
                "ck_outbox_messages_indeterminate_terminal",
                "indeterminate_terminal",
            )
            if name in checks
        ),
        None,
    )
    if terminal_check is not None:
        # The inspector returns the convention-expanded PostgreSQL identifier.
        # Mark it as final so Alembic does not apply ``ck_<table>_`` again.
        op.drop_constraint(op.f(terminal_check), "outbox_messages", type_="check")
    columns = {item["name"] for item in inspector.get_columns("outbox_messages")}
    if "indeterminate_at" in columns:
        op.drop_column("outbox_messages", "indeterminate_at")
    if "dispatch_started_at" in columns:
        op.drop_column("outbox_messages", "dispatch_started_at")
