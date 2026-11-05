"""Official DTEK identity; no polling while the source gate is closed."""

from time_tl.domain.power_outages.models import PowerProvider

from .base import PowerFetchErrorCode, PowerSourceGate
from .dtek_html_provider import DtekHtmlProvider

DTEK_SOURCE_GATE = PowerSourceGate(
    PowerProvider.DTEK, PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED
)


class DtekPowerScheduleProvider(DtekHtmlProvider):
    """No guessed AJAX endpoint, WAF bypass or third-party fallback."""
