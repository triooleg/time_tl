"""Infrastructure contracts and adapters for the alerts.in.ua poller."""

from .http import AsyncHttpTransport, HttpRequest, HttpResponse
from .lease import InMemorySingletonLease, PostgresAdvisoryLease, SingletonLease
from .provider import (
    AlertsInUaAdapter,
    FetchResult,
    OblastAlertsInUaAdapter,
    OperatorCircuitOpen,
)
from .rate_limit import NoCatchUpRateLimiter
from .repositories import AlertOutboxItem, AlertPollState, AlertRepository, InMemoryAlertRepository

__all__ = [
    "AlertOutboxItem",
    "AlertPollState",
    "AlertRepository",
    "AlertsInUaAdapter",
    "AsyncHttpTransport",
    "FetchResult",
    "HttpRequest",
    "HttpResponse",
    "InMemoryAlertRepository",
    "InMemorySingletonLease",
    "NoCatchUpRateLimiter",
    "OblastAlertsInUaAdapter",
    "OperatorCircuitOpen",
    "PostgresAdvisoryLease",
    "SingletonLease",
]
