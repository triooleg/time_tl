"""Persist restart-safe sanitized import proposal payloads.

Revision ID: 0003_pending_import_payload
Revises: 0002_alert_poll_failures
"""

from alembic import op

revision = "0003_pending_import_payload"
down_revision = "0002_alert_poll_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pending_import_sessions ADD COLUMN IF NOT EXISTS proposal_payload jsonb"
    )
    # Legacy rows contain no recoverable proposal. Failing them closed is safer
    # than inventing a payload or permitting a confirmation after restart.
    op.execute("DELETE FROM pending_import_sessions WHERE proposal_payload IS NULL")
    op.execute("ALTER TABLE pending_import_sessions ALTER COLUMN proposal_payload SET NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_import_replacement_token "
        "ON pending_import_sessions (replacement_token_hash) "
        "WHERE replacement_token_hash IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pending_import_replacement_token")
    op.execute("ALTER TABLE pending_import_sessions DROP COLUMN IF EXISTS proposal_payload")
