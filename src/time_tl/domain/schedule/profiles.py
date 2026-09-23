from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from .models import ScheduleProfile
from .resolver import ResolvedDay, preview


def kyiv_today(now: datetime | None = None) -> date:
    """Return the Europe/Kyiv date without requiring the optional tzdata wheel."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_utc = current.astimezone(UTC)
    year = current_utc.year
    march_last = date(year, 3, 31)
    october_last = date(year, 10, 31)
    march_sunday = march_last - timedelta(days=(march_last.weekday() + 1) % 7)
    october_sunday = october_last - timedelta(days=(october_last.weekday() + 1) % 7)
    dst_start = datetime.combine(march_sunday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=1)
    dst_end = datetime.combine(october_sunday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=1)
    offset = timedelta(hours=3 if dst_start <= current_utc < dst_end else 2)
    return (current_utc + offset).date()


class ProfileLimitReached(ValueError):
    pass


class ConfirmationError(ValueError):
    pass


@dataclass(frozen=True)
class ActivationProposal:
    token: str
    profile_id: str
    current_active_profile_id: str | None
    resolved_preview: tuple[ResolvedDay, ...]
    requires_confirmation: bool = True


class ProfileCollection:
    MAX_PROFILES = 3

    def __init__(self, profiles: tuple[ScheduleProfile, ...] = ()) -> None:
        if len(profiles) > self.MAX_PROFILES:
            raise ProfileLimitReached("a user can store at most three profiles")
        active_count = sum(profile.active for profile in profiles)
        if profiles and active_count != 1:
            raise ValueError("a non-empty profile collection must have exactly one active profile")
        self._profiles = tuple(profiles)
        self._pending_activation: ActivationProposal | None = None

    @property
    def profiles(self) -> tuple[ScheduleProfile, ...]:
        return self._profiles

    @property
    def active_profile(self) -> ScheduleProfile | None:
        return next((profile for profile in self._profiles if profile.active), None)

    def add(self, profile: ScheduleProfile) -> ScheduleProfile:
        if len(self._profiles) >= self.MAX_PROFILES:
            raise ProfileLimitReached("a user can store at most three profiles")
        if any(item.profile_id == profile.profile_id for item in self._profiles):
            raise ValueError("profile_id must be unique")
        if self._profiles and profile.active:
            raise ValueError("another profile is already active")
        if not self._profiles:
            profile = replace(profile, active=True)
        self._profiles += (profile,)
        return profile

    def request_activation(
        self, profile_id: str, *, start: date | None = None
    ) -> ActivationProposal:
        target = next(
            (profile for profile in self._profiles if profile.profile_id == profile_id), None
        )
        if target is None:
            raise KeyError(profile_id)
        proposal = ActivationProposal(
            token=uuid4().hex,
            profile_id=profile_id,
            current_active_profile_id=(
                self.active_profile.profile_id if self.active_profile else None
            ),
            resolved_preview=preview(target, start or kyiv_today(), 14),
        )
        self._pending_activation = proposal
        return proposal

    def confirm_activation(
        self, proposal: ActivationProposal, *, confirmed: bool
    ) -> ScheduleProfile:
        if not confirmed:
            raise ConfirmationError("activation was not confirmed")
        if proposal is not self._pending_activation:
            raise ConfirmationError("activation proposal is missing, stale, or already consumed")
        self._profiles = tuple(
            replace(profile, active=profile.profile_id == proposal.profile_id)
            for profile in self._profiles
        )
        self._pending_activation = None
        active = self.active_profile
        if active is None:
            raise AssertionError("activation did not produce an active profile")
        return active

    def replace(self, profile_id: str, replacement: ScheduleProfile) -> ScheduleProfile:
        target = next((item for item in self._profiles if item.profile_id == profile_id), None)
        if target is None:
            raise KeyError(profile_id)
        if any(item.profile_id == replacement.profile_id for item in self._profiles):
            raise ValueError("profile_id must be unique")
        replacement = replace(replacement, active=target.active)
        self._profiles = tuple(
            replacement if item.profile_id == profile_id else item for item in self._profiles
        )
        return replacement
