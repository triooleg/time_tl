"""Public metadata and local wire cache access; never decrypts credentials."""

from datetime import datetime
from typing import Any


class PowerMapAdminPort:
    def __init__(self, repository: Any, budget: Any) -> None:
        self._repository, self._budget = repository, budget

    async def statistics(self, now: datetime) -> dict[str, object]:
        return {
            "poll_interval_seconds": 300,
            "map": await self._repository.statistics(now),
            "api_budget": await self._budget.statistics(now),
            "user_notifications_enabled": False,
        }

    async def region(self, slug: str, now: datetime) -> dict[str, object]:
        value = await self._repository.read_region(slug, now)
        return {
            "source_status": value.source_status.value,
            "revision": value.revision,
            "error_code": value.error_code,
            "data": value.data.to_wire() if value.data is not None else None,
        }
