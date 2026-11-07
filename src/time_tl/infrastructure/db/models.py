from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    mode: Mapped[str | None] = mapped_column(String(32))
    confirmed_region_id: Mapped[str | None] = mapped_column(String(64))
    region_name: Mapped[str | None] = mapped_column(String(160))
    reminder_lead_minutes: Mapped[int] = mapped_column(Integer, server_default="5")
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    lessons_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    calendar_reminder_generation: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    __table_args__ = (
        CheckConstraint(
            "mode IN ('LESSONS_ONLY','LESSONS_AND_ALERTS','ALERTS_ONLY')", name="mode_allowed"
        ),
        CheckConstraint("reminder_lead_minutes BETWEEN 0 AND 120", name="reminder_lead_range"),
    )


class ScheduleProfile(TimestampMixin, Base):
    __tablename__ = "schedule_profiles"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    public_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), server_default="Europe/Kyiv")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (
        UniqueConstraint("user_id", "public_id"),
        CheckConstraint("timezone = 'Europe/Kyiv'", name="timezone_v1"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_nonempty"),
        Index(
            "uq_schedule_profiles_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class ScheduleRule(Base):
    __tablename__ = "schedule_rules"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("schedule_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    week_parity: Mapped[str] = mapped_column(String(8), server_default="ANY")
    date_parity: Mapped[str] = mapped_column(String(8), server_default="ANY")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        UniqueConstraint("profile_id", "position"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("week_parity IN ('ANY','ODD','EVEN')", name="week_parity_allowed"),
        CheckConstraint("date_parity IN ('ANY','ODD','EVEN')", name="date_parity_allowed"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from", name="date_range"
        ),
    )


class DateOverride(Base):
    __tablename__ = "date_overrides"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("schedule_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    day_off: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    __table_args__ = (UniqueConstraint("profile_id", "day"),)


class LessonMixin:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    join_url_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)


class RuleLesson(LessonMixin, Base):
    __tablename__ = "rule_lessons"
    rule_id: Mapped[UUID] = mapped_column(
        ForeignKey("schedule_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    __table_args__ = (
        UniqueConstraint("rule_id", "position"),
        CheckConstraint("end_time > start_time", name="time_order"),
        CheckConstraint("char_length(btrim(title)) > 0", name="title_nonempty"),
    )


class OverrideLesson(LessonMixin, Base):
    __tablename__ = "override_lessons"
    override_id: Mapped[UUID] = mapped_column(
        ForeignKey("date_overrides.id", ondelete="CASCADE"), nullable=False, index=True
    )
    __table_args__ = (
        UniqueConstraint("override_id", "position"),
        CheckConstraint("end_time > start_time", name="time_order"),
        CheckConstraint("char_length(btrim(title)) > 0", name="title_nonempty"),
    )


class PendingImportSession(TimestampMixin, Base):
    __tablename__ = "pending_import_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replacement_token_hash: Mapped[str | None] = mapped_column(String(64))
    replace_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("schedule_profiles.id", ondelete="CASCADE")
    )
    __table_args__ = (
        Index(
            "uq_pending_import_replacement_token",
            "replacement_token_hash",
            unique=True,
            postgresql_where=text("replacement_token_hash IS NOT NULL"),
        ),
        Index(
            "ix_pending_import_active_user_created",
            "user_id",
            "created_at",
            "token_hash",
            postgresql_where=text("revoked_at IS NULL AND consumed_at IS NULL"),
        ),
    )


class ShareSession(TimestampMixin, Base):
    __tablename__ = "share_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("schedule_profiles.id", ondelete="CASCADE"))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    include_join_links: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ScheduleSubject(TimestampMixin, Base):
    """Stable subject identity derived from one personal schedule profile."""

    __tablename__ = "schedule_subjects"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("schedule_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    __table_args__ = (
        UniqueConstraint("profile_id", "normalized_name"),
        UniqueConstraint("id", "profile_id", name="schedule_subject_identity"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_nonempty"),
        CheckConstraint("char_length(normalized_name) > 0", name="normalized_name_nonempty"),
    )


class HomeworkAssignment(TimestampMixin, Base):
    """One personal homework assignment; shared ownership is a later additive phase."""

    __tablename__ = "homework_assignments"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="MANUAL")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(
            ["subject_id", "profile_id"],
            ["schedule_subjects.id", "schedule_subjects.profile_id"],
            name="fk_homework_subject_profile",
            ondelete="CASCADE",
        ),
        CheckConstraint("char_length(btrim(text)) BETWEEN 1 AND 2000", name="text_length"),
        CheckConstraint("source_type = 'MANUAL'", name="source_type_allowed"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_homework_assignments_owner_due", "owner_user_id", "due_at", "id"),
    )


class HomeworkUserState(Base):
    """Per-user completion state, separate from shared assignment content."""

    __tablename__ = "homework_user_states"
    homework_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_assignments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "(completed AND completed_at IS NOT NULL) OR (NOT completed AND completed_at IS NULL)",
            name="completion_consistent",
        ),
        Index("ix_homework_user_states_user", "user_id", "homework_id"),
    )


class PendingHomeworkDraft(TimestampMixin, Base):
    """Restart-safe, expiring confirmation draft. Raw tokens are never persisted."""

    __tablename__ = "pending_homework_drafts"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="MANUAL")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_homework_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "homework_assignments.id",
            name="fk_pending_homework_confirmed_assignment",
            ondelete="CASCADE",
        ),
        unique=True,
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["subject_id", "profile_id"],
            ["schedule_subjects.id", "schedule_subjects.profile_id"],
            name="fk_pending_homework_subject_profile",
            ondelete="CASCADE",
        ),
        CheckConstraint("char_length(btrim(text)) BETWEEN 1 AND 2000", name="text_length"),
        CheckConstraint("source_type = 'MANUAL'", name="source_type_allowed"),
        CheckConstraint(
            "consumed_at IS NULL OR confirmed_homework_id IS NOT NULL",
            name="consumed_has_result",
        ),
        Index(
            "ix_pending_homework_active_user_created",
            "user_id",
            "created_at",
            "token_hash",
            postgresql_where=sa_text("revoked_at IS NULL AND consumed_at IS NULL"),
        ),
    )


class AlertPollState(Base):
    __tablename__ = "alert_poll_state"
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    fetch_revision: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_modified: Mapped[str | None] = mapped_column(String(160))
    circuit_open: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    retry_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="singleton"),
        CheckConstraint("fetch_revision >= 0", name="revision_nonnegative"),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
    )


class TerritoryCatalogVersion(Base):
    """Immutable metadata for one imported provider territory catalogue."""

    __tablename__ = "territory_catalog_versions"
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="DRAFT")
    source_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selectable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    hidden_nonselectable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_total: Mapped[int] = mapped_column(Integer, nullable=False)
    reconciliation_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','ACTIVE','RETIRED')", name="status_allowed"),
        CheckConstraint("record_count > 0", name="record_count_positive"),
        CheckConstraint(
            "selectable_count >= 0 AND selectable_count <= record_count",
            name="selectable_count_range",
        ),
        CheckConstraint(
            "hidden_nonselectable_count >= 0 AND hidden_nonselectable_count <= record_count",
            name="hidden_nonselectable_count_range",
        ),
        CheckConstraint(
            "selectable_count + hidden_nonselectable_count = record_count",
            name="reconciliation_counts_complete",
        ),
        CheckConstraint(
            "unresolved_total >= hidden_nonselectable_count",
            name="unresolved_total_covers_hidden",
        ),
        CheckConstraint("char_length(source_sha256) = 64", name="source_sha256_length"),
        Index(
            "uq_territory_catalog_versions_one_active",
            "provider",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class Territory(Base):
    """One provider UID in a validated, immutable hierarchy revision."""

    __tablename__ = "territories"
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(96), primary_key=True)
    region_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    parent_region_id: Mapped[str | None] = mapped_column(String(64))
    external_code: Mapped[str | None] = mapped_column(String(64))
    sort_key: Mapped[str] = mapped_column(String(240), nullable=False)
    is_selectable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "catalog_version"],
            ["territory_catalog_versions.provider", "territory_catalog_versions.catalog_version"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["provider", "catalog_version", "parent_region_id"],
            ["territories.provider", "territories.catalog_version", "territories.region_id"],
            name="fk_territories_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "kind IN ('OBLAST','SPECIAL_STATUS_CITY','RAION','HROMADA')",
            name="kind_allowed",
        ),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_nonempty"),
        Index("ix_territories_parent", "provider", "catalog_version", "parent_region_id"),
    )


class ActiveTerritoryCatalog(Base):
    """Single atomic pointer to the catalogue currently served for a provider."""

    __tablename__ = "active_territory_catalogs"
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(96), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "catalog_version"],
            ["territory_catalog_versions.provider", "territory_catalog_versions.catalog_version"],
            ondelete="RESTRICT",
        ),
    )


class AlertScope(Base):
    __tablename__ = "alert_scopes"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    selected_region_id: Mapped[str] = mapped_column(String(64), nullable=False)
    applicable_region_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    territory_provider: Mapped[str | None] = mapped_column(String(64))
    territory_catalog_version: Mapped[str | None] = mapped_column(String(96))
    __table_args__ = (
        ForeignKeyConstraint(
            ["territory_provider", "territory_catalog_version", "selected_region_id"],
            ["territories.provider", "territories.catalog_version", "territories.region_id"],
            name="fk_alert_scopes_selected_territory",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(territory_provider IS NULL) = (territory_catalog_version IS NULL)",
            name="territory_revision_pair",
        ),
    )


class AlertSnapshot(Base):
    __tablename__ = "alert_snapshots"
    scope_id: Mapped[UUID] = mapped_column(
        ForeignKey("alert_scopes.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(24), server_default="UNKNOWN")
    last_fetch_revision: Mapped[int | None] = mapped_column(BigInteger)
    transition_revision: Mapped[int | None] = mapped_column(BigInteger)
    last_full_active_episodes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    processed_update_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    pending_clear_candidate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    __table_args__ = (
        CheckConstraint(
            "state IN ('UNKNOWN','ACTIVE','STALE','CLEAR_CONFIRMED')", name="state_allowed"
        ),
        CheckConstraint(
            "last_fetch_revision IS NULL OR last_fetch_revision >= 0", name="revision_nonnegative"
        ),
    )


class AlertEpisode(Base):
    __tablename__ = "alert_episodes"
    scope_id: Mapped[UUID] = mapped_column(
        ForeignKey("alert_snapshots.scope_id", ondelete="CASCADE"), primary_key=True
    )
    location_uid: Mapped[str] = mapped_column(String(64), primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (CheckConstraint("active_revision >= 0", name="revision_nonnegative"),)


class OutboxMessage(TimestampMixin, Base):
    __tablename__ = "outbox_messages"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indeterminate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "indeterminate_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dead_at IS NOT NULL "
            "AND delivered_at IS NULL)",
            name="indeterminate_terminal",
        ),
        Index(
            "ix_outbox_pending",
            "available_at",
            postgresql_where=text("delivered_at IS NULL AND dead_at IS NULL"),
        ),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index(
            "ix_outbox_confirmed_clear_cursor",
            "created_at",
            "id",
            postgresql_where=text("event_type = 'ALL_CLEAR_CONFIRMED'"),
        ),
        Index(
            "ix_outbox_delivered_retention",
            "delivered_at",
            "id",
            postgresql_where=text(
                "delivered_at IS NOT NULL AND dead_at IS NULL AND claimed_at IS NULL"
            ),
        ),
        Index(
            "ix_outbox_dead_retention",
            "dead_at",
            "id",
            postgresql_where=text(
                "dead_at IS NOT NULL AND delivered_at IS NULL AND claimed_at IS NULL"
            ),
        ),
        Index(
            "ix_outbox_indeterminate",
            "indeterminate_at",
            "id",
            postgresql_where=text("indeterminate_at IS NOT NULL AND delivered_at IS NULL"),
        ),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class RuntimeSetting(TimestampMixin, Base):
    __tablename__ = "runtime_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(80), nullable=False)


class AdminAccount(TimestampMixin, Base):
    __tablename__ = "admin_accounts"
    username: Mapped[str] = mapped_column(String(80), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    bootstrap_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(
        ForeignKey("admin_accounts.username", ondelete="CASCADE"), index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminLoginAttempt(Base):
    __tablename__ = "admin_login_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False)
    source_ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), index=True
    )
    __table_args__ = (
        Index("ix_admin_login_lockout", "username", "source_ip_hash", "attempted_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), index=True
    )
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(128))
    redacted_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class DailyAggregateStat(Base):
    __tablename__ = "daily_aggregate_stats"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (CheckConstraint("value >= 0", name="value_nonnegative"),)


class Calendar(TimestampMixin, Base):
    """One independently created, link-free shared schedule snapshot."""

    __tablename__ = "calendars"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    owner_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        CheckConstraint("kind IN ('SCHOOL','PERSONAL','ACTIVITY')", name="kind_allowed"),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("snapshot_version >= 1", name="snapshot_version_positive"),
        CheckConstraint("jsonb_typeof(snapshot_payload) = 'object'", name="snapshot_object"),
    )


class CalendarMembership(TimestampMixin, Base):
    __tablename__ = "calendar_memberships"
    calendar_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="MEMBER")
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    __table_args__ = (CheckConstraint("role IN ('MEMBER','EDITOR','ADMIN')", name="role_allowed"),)


class CalendarInvitation(Base):
    """Targeted token hash, never a raw token or message delivery instruction."""

    __tablename__ = "calendar_invitations"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    calendar_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issuer_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    calendar_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_calendar_invitations_expiry_token", "expires_at", "token_hash"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_shape"),
        CheckConstraint("calendar_version >= 1", name="version_positive"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '24 hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(
            "NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)", name="terminal"
        ),
    )


class PersonalCalendarOverride(TimestampMixin, Base):
    """Actor-only occurrence change; RESET rows retain their CAS tombstone."""

    __tablename__ = "personal_calendar_overrides"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    calendar_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_day: Mapped[date] = mapped_column(Date, primary_key=True)
    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_day: Mapped[date | None] = mapped_column(Date)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    __table_args__ = (
        Index("ix_personal_calendar_overrides_calendar_id", "calendar_id"),
        CheckConstraint("snapshot_version BETWEEN 1 AND 2147483646", name="snapshot_range"),
        CheckConstraint("version BETWEEN 1 AND 2147483646", name="version_range"),
        CheckConstraint("action IN ('CANCEL','MOVE','RESET')", name="action_allowed"),
        CheckConstraint(
            "(action = 'MOVE' AND target_day IS NOT NULL AND start_time IS NOT NULL "
            "AND end_time IS NOT NULL AND end_time > start_time) OR "
            "(action IN ('CANCEL','RESET') AND target_day IS NULL "
            "AND start_time IS NULL AND end_time IS NULL)",
            name="action_shape",
        ),
        CheckConstraint(
            "(start_time IS NULL OR EXTRACT(SECOND FROM start_time) = 0) AND "
            "(end_time IS NULL OR EXTRACT(SECOND FROM end_time) = 0)",
            name="minute_precision",
        ),
    )


class PowerProviderState(TimestampMixin, Base):
    __tablename__ = "power_providers"
    provider_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    official_url: Mapped[str] = mapped_column(String(240), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    refresh_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    __table_args__ = (
        CheckConstraint(
            "provider_id IN ('dtek_dnipro','dtek_kyiv','dtek_kyiv_region','dtek_odesa',"
            "'dtek_donetsk','cek_dnipro')",
            name="provider_allowed",
        ),
        CheckConstraint(
            "source_status IN ('FRESH','STALE','UNAVAILABLE','INVALID')", name="status_allowed"
        ),
        CheckConstraint("consecutive_failures >= 0", name="failure_count"),
        CheckConstraint("configuration_revision >= 1", name="configuration_revision"),
    )


class PowerScheduleSnapshotRow(TimestampMixin, Base):
    __tablename__ = "power_schedule_snapshots"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("power_providers.provider_id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[str] = mapped_column(String(3), nullable=False)
    valid_for_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="official")
    publication_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PLANNED"
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    __table_args__ = (
        UniqueConstraint("provider_id", "group_id", "valid_for_date", "schedule_type", "revision"),
        CheckConstraint("group_id ~ '^[1-6]\\.[12]$'", name="group_allowed"),
        CheckConstraint(
            "schedule_type IN ('CONFIRMED_DAY','FORECAST_WEEK')", name="schedule_type_allowed"
        ),
        CheckConstraint("revision >= 1 AND schema_version = 1", name="version_allowed"),
        CheckConstraint("source_kind = 'official'", name="official_only"),
        CheckConstraint(
            "publication_status IN ('PLANNED','NO_SCHEDULE','EMERGENCY','UNKNOWN')",
            name="publication_allowed",
        ),
        Index(
            "uq_power_current_schedule",
            "provider_id",
            "group_id",
            "valid_for_date",
            "schedule_type",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )


class PowerScheduleIntervalRow(Base):
    __tablename__ = "power_schedule_intervals"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("power_schedule_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[str] = mapped_column(String(3), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="positive_duration"),
        CheckConstraint(
            "state IN ('OUTAGE_CONFIRMED','POWER_EXPECTED','POSSIBLE_OUTAGE',"
            "'UNKNOWN','NO_SCHEDULE')",
            name="state_allowed",
        ),
        CheckConstraint("group_id ~ '^[1-6]\\.[12]$'", name="group_allowed"),
    )


class UserPowerSubscription(TimestampMixin, Base):
    __tablename__ = "user_power_subscriptions"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("power_providers.provider_id"), nullable=False
    )
    group_id: Mapped[str] = mapped_column(String(3), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    outage_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    restoration_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    outage_lead_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    restoration_lead_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        CheckConstraint("group_id ~ '^[1-6]\\.[12]$'", name="group_allowed"),
        CheckConstraint(
            "outage_lead_minutes >= 0 AND outage_lead_minutes <= 180 AND "
            "restoration_lead_minutes >= 0 AND restoration_lead_minutes <= 180",
            name="lead_allowed",
        ),
        CheckConstraint("revision >= 1", name="revision_allowed"),
        Index("ix_power_subscriptions_provider_group", "provider_id", "group_id"),
    )


class PowerNotificationEvent(TimestampMixin, Base):
    __tablename__ = "power_notification_events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_power_subscriptions.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("power_schedule_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    outbox_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("outbox_messages.id", ondelete="SET NULL"), unique=True
    )
    event_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    subscription_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    transition_type: Mapped[str] = mapped_column(String(40), nullable=False)
    transition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','SENT','CANCELED')", name="status_allowed"),
        CheckConstraint(
            "transition_type IN ('POWER_OUTAGE_UPCOMING','POWER_RESTORATION_UPCOMING',"
            "'POWER_SCHEDULE_CHANGED')",
            name="transition_allowed",
        ),
        CheckConstraint(
            "subscription_revision >= 1 AND schedule_revision >= 1", name="revisions_allowed"
        ),
        Index("ix_power_events_snapshot_status", "snapshot_id", "status"),
    )


class WeatherLocation(TimestampMixin, Base):
    __tablename__ = "weather_locations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    katottg_code: Mapped[str] = mapped_column(String(19), nullable=False, index=True)
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    locality_name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    coverage_type: Mapped[str] = mapped_column(String(16), server_default="BASE", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    provenance_url: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_version: Mapped[str] = mapped_column(String(80), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("latitude BETWEEN 44 AND 53.5", name="ukraine_latitude"),
        CheckConstraint("longitude BETWEEN 22 AND 41", name="ukraine_longitude"),
        CheckConstraint("katottg_code ~ '^UA[0-9]{17}$'", name="katottg_shape"),
        CheckConstraint("coverage_type IN ('BASE','EXTENDED')", name="coverage_allowed"),
        CheckConstraint("NOT enabled OR approved_at IS NOT NULL", name="approval_required"),
        CheckConstraint("length(provenance_sha256)=64", name="provenance_digest"),
        Index(
            "uq_weather_base_location",
            "katottg_code",
            unique=True,
            postgresql_where=text("coverage_type='BASE'"),
        ),
    )


class WeatherForecastRun(Base):
    __tablename__ = "weather_forecast_runs"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_locations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    successful_locations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_locations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    __table_args__ = (
        CheckConstraint(
            "requested_locations>=0 AND successful_locations>=0 AND failed_locations>=0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "successful_locations+failed_locations<=requested_locations", name="counts_bounded"
        ),
        Index("ix_weather_runs_provider_started", "provider", "started_at"),
    )


class WeatherForecastSnapshot(Base):
    __tablename__ = "weather_forecast_snapshots"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    weather_location_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("weather_locations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    forecast_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("weather_forecast_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cache_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forecast_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    last_modified: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        UniqueConstraint("weather_location_id", "provider", "forecast_hash"),
        CheckConstraint("status IN ('FRESH','STALE','PARTIAL','UNKNOWN')", name="status_allowed"),
        Index(
            "ix_weather_snapshot_location_fetched", "weather_location_id", "provider", "fetched_at"
        ),
    )


class WeatherForecastHour(Base):
    __tablename__ = "weather_forecast_hours"
    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("weather_forecast_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    forecast_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    feels_like_c: Mapped[float | None] = mapped_column(Float)
    precipitation_mm: Mapped[float | None] = mapped_column(Float)
    precipitation_probability: Mapped[float | None] = mapped_column(Float)
    snowfall_cm: Mapped[float | None] = mapped_column(Float)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float)
    wind_gust_ms: Mapped[float | None] = mapped_column(Float)
    cloud_cover_percent: Mapped[float | None] = mapped_column(Float)
    weather_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("temperature_c BETWEEN -100 AND 70", name="temperature_range"),
        CheckConstraint("precipitation_mm>=0 AND snowfall_cm>=0", name="precipitation_nonnegative"),
        CheckConstraint("precipitation_probability BETWEEN 0 AND 100", name="probability_range"),
        CheckConstraint("wind_speed_ms>=0 AND wind_gust_ms>=0", name="wind_nonnegative"),
        CheckConstraint("cloud_cover_percent BETWEEN 0 AND 100", name="cloud_range"),
        Index("ix_weather_hours_valid_time", "forecast_at"),
    )


class WeatherChangeEvent(Base):
    __tablename__ = "weather_change_events"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    location_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("weather_locations.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("weather_forecast_snapshots.id", ondelete="SET NULL")
    )
    event_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    previous_value: Mapped[float | None] = mapped_column(Float)
    current_value: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ADVISORY")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    __table_args__ = (
        CheckConstraint("expected_end>expected_start", name="valid_interval"),
        CheckConstraint("kind IN ('UPCOMING','CORRECTION','CANCELLATION')", name="kind_allowed"),
        CheckConstraint("status IN ('ACTIVE','CANCELLED','EXPIRED')", name="status_allowed"),
        Index("ix_weather_events_location_time", "location_id", "expected_start", "status"),
    )


class UserWeatherPreference(TimestampMixin, Base):
    __tablename__ = "user_weather_preferences"
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    forecast_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    weather_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    daily_forecast_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    daily_forecast_time: Mapped[time] = mapped_column(Time, nullable=False, server_default="07:00")
    weather_changes_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    rain_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    snow_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    wind_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    temperature_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    frost_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    notification_lead_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="120"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        CheckConstraint("notification_lead_minutes BETWEEN 0 AND 1440", name="lead_range"),
        CheckConstraint("revision>=1", name="revision_positive"),
        Index("ix_weather_preferences_enabled", "weather_enabled", "forecast_requested"),
    )


class WeatherNotificationDelivery(Base):
    __tablename__ = "weather_notification_deliveries"
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    notification_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    outbox_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("outbox_messages.id", ondelete="SET NULL")
    )
    event_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertHistoryPeriod(Base):
    __tablename__ = "alert_history_periods"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope_id: Mapped[UUID] = mapped_column(
        ForeignKey("alert_scopes.id", ondelete="CASCADE"), nullable=False
    )
    binding_key: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_gaps: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    __table_args__ = (
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="valid_interval"),
        CheckConstraint(
            "tracking_ended_at IS NULL OR tracking_ended_at >= started_at", name="valid_tracking"
        ),
        Index("ix_alert_history_period_scope", "scope_id", "binding_key", "started_at"),
    )


class AlertHistorySegment(Base):
    __tablename__ = "alert_history_segments"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope_id: Mapped[UUID] = mapped_column(
        ForeignKey("alert_scopes.id", ondelete="CASCADE"), nullable=False
    )
    binding_key: Mapped[str] = mapped_column(String(64), nullable=False)
    period_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("alert_history_periods.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    __table_args__ = (
        CheckConstraint("ended_at >= started_at", name="valid_interval"),
        CheckConstraint("active = (period_id IS NOT NULL)", name="active_period"),
        Index("ix_alert_history_segment_scope", "scope_id", "binding_key", "ended_at"),
    )


class AlertHistoryCursor(Base):
    __tablename__ = "alert_history_cursors"
    scope_id: Mapped[UUID] = mapped_column(
        ForeignKey("alert_scopes.id", ondelete="CASCADE"), primary_key=True
    )
    binding_key: Mapped[str] = mapped_column(String(64), nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    period_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("alert_history_periods.id", ondelete="SET NULL")
    )
    segment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("alert_history_segments.id", ondelete="SET NULL")
    )
    __table_args__ = (
        CheckConstraint(
            "state IN ('UNKNOWN','STALE','ACTIVE','CLEAR_CONFIRMED')", name="state_allowed"
        ),
    )
