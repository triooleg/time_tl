"""Unsupported sources cannot silently replace the approved forecast source."""

from dataclasses import dataclass

from .met_norway import WeatherProviderError


@dataclass(frozen=True)
class OpenMeteoPermission:
    enabled: bool = False
    usage_verified: bool = False
    license_verified: bool = False
    endpoint_verified: bool = False

    def require(self) -> None:
        if not (
            self.enabled
            and self.usage_verified
            and self.license_verified
            and self.endpoint_verified
        ):
            raise WeatherProviderError("NOT_CONFIGURED")
        raise WeatherProviderError("ADAPTER_NOT_IMPLEMENTED")


def official_warnings() -> None:
    raise WeatherProviderError("OFFICIAL_FEED_NOT_VERIFIED")
