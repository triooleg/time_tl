"""Verified top-level location registry from the official alerts.in.ua docs.

The provider documents these UID values for oblasts and special-status cities.
This registry deliberately does not claim coverage for raions, hromadas, or
ordinary cities. It is reviewed only for the official aggregate-oblast
endpoint: each confirmed user scope is exactly one of these UIDs. Production
coordinate resolution and public release remain separately gated.

Source: https://devs.alerts.in.ua/ (section "Location UID"), verified 2026-09-20.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRegion:
    uid: str
    name_uk: str


OBLAST_REGIONS: tuple[ProviderRegion, ...] = (
    ProviderRegion("3", "Хмельницька область"),
    ProviderRegion("4", "Вінницька область"),
    ProviderRegion("5", "Рівненська область"),
    ProviderRegion("8", "Волинська область"),
    ProviderRegion("9", "Дніпропетровська область"),
    ProviderRegion("10", "Житомирська область"),
    ProviderRegion("11", "Закарпатська область"),
    ProviderRegion("12", "Запорізька область"),
    ProviderRegion("13", "Івано-Франківська область"),
    ProviderRegion("14", "Київська область"),
    ProviderRegion("15", "Кіровоградська область"),
    ProviderRegion("16", "Луганська область"),
    ProviderRegion("17", "Миколаївська область"),
    ProviderRegion("18", "Одеська область"),
    ProviderRegion("19", "Полтавська область"),
    ProviderRegion("20", "Сумська область"),
    ProviderRegion("21", "Тернопільська область"),
    ProviderRegion("22", "Харківська область"),
    ProviderRegion("23", "Херсонська область"),
    ProviderRegion("24", "Черкаська область"),
    ProviderRegion("25", "Чернігівська область"),
    ProviderRegion("26", "Чернівецька область"),
    ProviderRegion("27", "Львівська область"),
    ProviderRegion("28", "Донецька область"),
    ProviderRegion("29", "Автономна Республіка Крим"),
    ProviderRegion("30", "м. Севастополь"),
    ProviderRegion("31", "м. Київ"),
)
OBLAST_REGION_IDS = frozenset(item.uid for item in OBLAST_REGIONS)
REGISTRY_SCOPE = "oblasts_and_special_status_cities_only"
REGISTRY_IS_COMPLETE_HIERARCHY = False
REGISTRY_SUPPORTS_AGGREGATE_OBLAST_SCOPES = True
