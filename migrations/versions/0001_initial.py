"""Initial durable Time TL schema. Revision ID: 0001_initial."""

from alembic import op

from time_tl.infrastructure.db import models  # noqa: F401
from time_tl.infrastructure.db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None
PROFILE_LIMIT_FUNCTION = """
CREATE FUNCTION enforce_profile_limit() RETURNS trigger AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.user_id::text, 0));
  IF (SELECT count(*) FROM schedule_profiles WHERE user_id = NEW.user_id) >= 3 THEN
    RAISE EXCEPTION 'a user can store at most three schedule profiles' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
PROFILE_LIMIT_TRIGGER = """
CREATE TRIGGER trg_schedule_profile_limit BEFORE INSERT ON schedule_profiles
FOR EACH ROW EXECUTE FUNCTION enforce_profile_limit();
"""
ACTIVE_PROFILE_FUNCTION = """
CREATE FUNCTION ensure_one_active_profile() RETURNS trigger AS $$
DECLARE affected_user uuid;
BEGIN
  affected_user := COALESCE(NEW.user_id, OLD.user_id);
  IF EXISTS (SELECT 1 FROM schedule_profiles WHERE user_id=affected_user)
     AND (SELECT count(*) FROM schedule_profiles
          WHERE user_id=affected_user AND is_active) <> 1 THEN
    RAISE EXCEPTION
      'a non-empty profile collection must have exactly one active profile'
      USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""
ACTIVE_PROFILE_TRIGGER = """
CREATE CONSTRAINT TRIGGER trg_schedule_one_active
AFTER INSERT OR UPDATE OR DELETE ON schedule_profiles
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION ensure_one_active_profile();
"""
DAY_OFF_FUNCTION = """
CREATE FUNCTION reject_day_off_lesson() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM date_overrides WHERE id=NEW.override_id AND day_off) THEN
    RAISE EXCEPTION 'a day-off override cannot contain lessons' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
DAY_OFF_TRIGGER = """
CREATE TRIGGER trg_no_day_off_lesson BEFORE INSERT OR UPDATE ON override_lessons
FOR EACH ROW EXECUTE FUNCTION reject_day_off_lesson();
"""
DAY_OFF_UPDATE_FUNCTION = """
CREATE FUNCTION reject_day_off_with_lessons() RETURNS trigger AS $$
BEGIN
  IF NEW.day_off AND EXISTS (SELECT 1 FROM override_lessons WHERE override_id=NEW.id) THEN
    RAISE EXCEPTION 'a day-off override cannot contain lessons' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
DAY_OFF_UPDATE_TRIGGER = """
CREATE TRIGGER trg_no_lessons_when_day_off BEFORE UPDATE OF day_off ON date_overrides
FOR EACH ROW EXECUTE FUNCTION reject_day_off_with_lessons();
"""


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)
    op.execute(PROFILE_LIMIT_FUNCTION)
    op.execute(PROFILE_LIMIT_TRIGGER)
    op.execute(ACTIVE_PROFILE_FUNCTION)
    op.execute(ACTIVE_PROFILE_TRIGGER)
    op.execute(DAY_OFF_FUNCTION)
    op.execute(DAY_OFF_TRIGGER)
    op.execute(DAY_OFF_UPDATE_FUNCTION)
    op.execute(DAY_OFF_UPDATE_TRIGGER)
    op.execute("INSERT INTO alert_poll_state(singleton_id, fetch_revision) VALUES (1,0)")


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS reject_day_off_with_lessons() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_day_off_lesson() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS ensure_one_active_profile() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS enforce_profile_limit() CASCADE")
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
