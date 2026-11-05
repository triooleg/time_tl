"""Persist the verified alert transition used to authorize queued messages."""

import sqlalchemy as sa
from alembic import op

revision = "0013_alert_transition_revision"
down_revision = "0012_weather_coverage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 uses current model metadata when installing a fresh database.
    op.execute("ALTER TABLE alert_snapshots ADD COLUMN IF NOT EXISTS transition_revision BIGINT")
    validate_schema(op.get_bind())


def validate_schema(connection: sa.Connection) -> None:
    column = next(
        item for item in sa.inspect(connection).get_columns("alert_snapshots")
        if item["name"] == "transition_revision"
    )
    if not isinstance(column["type"], sa.BigInteger) or not column["nullable"] or column["default"]:
        raise RuntimeError("Unexpected alert transition revision column")


def downgrade() -> None:
    op.drop_column("alert_snapshots", "transition_revision")
