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
        ForeignKey("homework_assignments.id", ondelete="CASCADE"), unique=True
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
            postgresql_where=text(
                "indeterminate_at IS NOT NULL AND delivered_at IS NULL"
            ),
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
