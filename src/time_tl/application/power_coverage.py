"""Exact registry-bound power demand; never fuzzy geographic inference."""

from __future__ import annotations

from time_tl.domain.power_outages.map import RegionMap
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.power_outages.alerts_energy_api_normalizer import KNOWN_BINDINGS

PowerGroups = frozenset[tuple[PowerProvider, str]]


def demanded_areas(groups: PowerGroups) -> frozenset[str]:
    return frozenset(
        KNOWN_BINDINGS[provider][0] for provider, _ in groups if provider in KNOWN_BINDINGS
    )


def retain_demanded_groups(snapshot: RegionMap, groups: PowerGroups) -> RegionMap:
    identities = {
        (KNOWN_BINDINGS[provider][1], group)
        for provider, group in groups
        if provider in KNOWN_BINDINGS and KNOWN_BINDINGS[provider][0] == snapshot.area.slug
    }
    wire = snapshot.to_wire()
    wire["rows"] = [row for row in wire["rows"] if (row["initiator"], row["queue"]) in identities]
    return RegionMap.from_wire(wire)
