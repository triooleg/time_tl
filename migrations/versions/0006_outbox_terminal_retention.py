"""Index bounded terminal outbox retention.

Revision ID: 0006_outbox_terminal_retention
Revises: 0005_public_data_indexes
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0006_outbox_terminal_retention"
down_revision = "0005_public_data_indexes"
branch_labels = None
depends_on = None

_INDEXES = (
    (
        "ix_outbox_delivered_retention",
        ["delivered_at", "id"],
        sa.text("delivered_at IS NOT NULL AND dead_at IS NULL AND claimed_at IS NULL"),
    ),
    (
        "ix_outbox_dead_retention",
        ["dead_at", "id"],
        sa.text("dead_at IS NOT NULL AND delivered_at IS NULL AND claimed_at IS NULL"),
    ),
)


def upgrade() -> None:
    if context.is_offline_mode():
        # Historical revision 0001 renders current Base.metadata for a complete
        # from-scratch offline script, including these indexes.
        return
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_indexes("outbox_messages")}
    for name, columns, predicate in _INDEXES:
        if name not in existing:
            op.create_index(
                name,
                "outbox_messages",
                columns,
                unique=False,
                postgresql_where=predicate,
            )


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_indexes("outbox_messages")}
    for name, _columns, _predicate in reversed(_INDEXES):
        if name in existing:
            op.drop_index(name, table_name="outbox_messages")
