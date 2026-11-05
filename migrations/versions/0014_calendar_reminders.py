"""Add a default-off opt-in generation for personal calendar reminders."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0014_calendar_reminders"
down_revision = "0013_alert_transition_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 uses current model metadata when installing a fresh database.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS calendar_reminder_generation UUID")
    validate_schema(op.get_bind())


def validate_schema(connection: sa.Connection) -> None:
    column = next(
        item for item in sa.inspect(connection).get_columns("users")
        if item["name"] == "calendar_reminder_generation"
    )
    if not isinstance(column["type"], UUID) or not column["nullable"] or column["default"]:
        raise RuntimeError("Unexpected calendar reminder generation column")


def downgrade() -> None:
    op.drop_column("users", "calendar_reminder_generation")
