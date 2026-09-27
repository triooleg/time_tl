"""Add bounded public-data lookup indexes.

Revision ID: 0005_public_data_indexes
Revises: 0004_territory_catalog

The migration is idempotent for this repository's historical 0001
``Base.metadata.create_all()`` behaviour: a new database may already contain
current-model indexes before Alembic reaches this revision.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0005_public_data_indexes"
down_revision = "0004_territory_catalog"
branch_labels = None
depends_on = None

_INDEXES = (
    (
        "ix_outbox_aggregate",
        "outbox_messages",
        ["aggregate_type", "aggregate_id"],
        None,
    ),
    (
        "ix_outbox_confirmed_clear_cursor",
        "outbox_messages",
        ["created_at", "id"],
        sa.text("event_type = 'ALL_CLEAR_CONFIRMED'"),
    ),
    (
        "ix_pending_import_active_user_created",
        "pending_import_sessions",
        ["user_id", "created_at", "token_hash"],
        sa.text("revoked_at IS NULL AND consumed_at IS NULL"),
    ),
)


def upgrade() -> None:
    if context.is_offline_mode():
        # Current-model indexes are already emitted by historical revision 0001
        # when generating a complete offline script from base.
        return
    inspector = sa.inspect(op.get_bind())
    for name, table_name, columns, predicate in _INDEXES:
        existing = {item["name"] for item in inspector.get_indexes(table_name)}
        if name in existing:
            continue
        options = {"postgresql_where": predicate} if predicate is not None else {}
        op.create_index(name, table_name, columns, unique=False, **options)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    for name, table_name, _columns, _predicate in reversed(_INDEXES):
        existing = {item["name"] for item in inspector.get_indexes(table_name)}
        if name in existing:
            op.drop_index(name, table_name=table_name)
