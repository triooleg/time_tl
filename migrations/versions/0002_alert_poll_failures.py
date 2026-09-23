"""Persist alert-worker failure/backoff state across restarts.

Revision ID: 0002_alert_poll_failures
Revises: 0001_initial
"""

from alembic import op

revision = "0002_alert_poll_failures"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 builds from current metadata in fresh installations, hence IF NOT
    # EXISTS keeps both fresh installs and upgrades from the earlier schema safe.
    op.execute(
        "ALTER TABLE alert_poll_state "
        "ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        DO $$ BEGIN
          ALTER TABLE alert_poll_state
            ADD CONSTRAINT ck_alert_poll_state_failures_nonnegative
            CHECK (consecutive_failures >= 0);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE alert_poll_state "
        "DROP CONSTRAINT IF EXISTS ck_alert_poll_state_failures_nonnegative"
    )
    op.execute("ALTER TABLE alert_poll_state DROP COLUMN IF EXISTS consecutive_failures")
