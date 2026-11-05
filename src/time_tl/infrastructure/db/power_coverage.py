"""Current power subscriptions fenced by their existing user/preference locks."""

from __future__ import annotations

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession

from time_tl.application.power_coverage import PowerGroups
from time_tl.domain.power_outages.models import GROUPS, PowerProvider

from .models import RuntimeSetting, User
from .power_map_preferences import PREFIX, decode


async def locked_power_groups(session: AsyncSession) -> PowerGroups:
    identifiers = (
        (
            await session.execute(
                select(User.id)
                .join(RuntimeSetting, RuntimeSetting.key == PREFIX + User.id.cast(String))
                .order_by(User.id)
                .with_for_update(of=User)
            )
        )
        .scalars()
        .all()
    )
    if not identifiers:
        return frozenset()
    rows = (
        (
            await session.execute(
                select(RuntimeSetting)
                .where(
                    RuntimeSetting.key.in_([PREFIX + str(identifier) for identifier in identifiers])
                )
                .order_by(RuntimeSetting.key)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    result: set[tuple[PowerProvider, str]] = set()
    for row in rows:
        try:
            preference = decode(row.value, row.key.removeprefix(PREFIX))
            # Delivery requires a verified full operator projection. The fetch
            # is already area-wide; retain all its groups for opted-in alerts.
            groups = GROUPS if preference.enabled else (preference.group_id,)
            result.update((preference.provider, group) for group in groups)
        except ValueError:
            continue  # Invalid preferences never authorize fetching/retaining a group.
    return frozenset(result)
