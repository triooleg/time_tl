"""Persist observed aggregate alert periods and coverage, without backfill."""

from alembic import op
from sqlalchemy import inspect

from time_tl.infrastructure.db.models import (
    AlertHistoryCursor,
    AlertHistoryPeriod,
    AlertHistorySegment,
)

revision = "0015_alert_history"
down_revision = "0014_calendar_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh 0001 already creates current metadata. Existing databases gain
    # only these three empty tables; absent historic evidence is not invented.
    connection = op.get_bind()
    for model in (AlertHistoryPeriod, AlertHistorySegment, AlertHistoryCursor):
        model.__table__.create(connection, checkfirst=True)
    validate_schema(connection)


def validate_schema(connection) -> None:
    metadata = inspect(connection)
    for model in (AlertHistoryPeriod, AlertHistorySegment, AlertHistoryCursor):
        actual = {column["name"] for column in metadata.get_columns(model.__tablename__)}
        if actual != set(model.__table__.columns.keys()):
            raise RuntimeError("Unexpected alert history schema")


def downgrade() -> None:
    # Export history first if it must be retained; application rollback can
    # keep these additive tables and need not execute this destructive step.
    for model in (AlertHistoryCursor, AlertHistorySegment, AlertHistoryPeriod):
        model.__table__.drop(op.get_bind(), checkfirst=True)
