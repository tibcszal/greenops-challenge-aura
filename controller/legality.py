"""Residency and profile legality.

A residency-invalid placement permanently disqualifies the run, even when the API
rejects it. Every create must pass assert_legal() first; nothing else in the
controller is allowed to decide where a workload may run.
"""
from __future__ import annotations

from .catalog import Catalog, Profile, Workload, Zone

EU_EXCLUDES_CH = "Switzerland is outside EU residency"


def zone_allowed(workload: Workload, zone: Zone) -> bool:
    if zone.country in workload.forbidden_countries:
        return False
    if workload.allowed_countries and zone.country not in workload.allowed_countries:
        return False
    if workload.residency == "EU":
        return zone.eu_member
    if workload.residency == "CH":
        return zone.country == "CH"
    if workload.residency == "ANYWHERE":
        return True
    raise ValueError(f"Unknown residency {workload.residency!r} for {workload.id}")


def profile_allowed(workload: Workload, profile: Profile) -> bool:
    if profile.work_unit != workload.work_unit:
        return False
    if profile.family not in workload.allowed_families:
        return False
    if workload.allowed_profile_ids and profile.id not in workload.allowed_profile_ids:
        return False
    if workload.minimum_gpu_memory_gib:
        if profile.gpu_memory_gib is None or profile.gpu_memory_gib < workload.minimum_gpu_memory_gib:
            return False
    return True


def legal_zones(workload: Workload, catalog: Catalog) -> list[Zone]:
    return [z for z in catalog.zones if zone_allowed(workload, z)]


def legal_profiles(workload: Workload, catalog: Catalog) -> list[Profile]:
    return [p for p in catalog.profiles if profile_allowed(workload, p)]


def legal_placements(workload: Workload, catalog: Catalog) -> list[tuple[Profile, Zone]]:
    """Every (profile, zone) pair this workload may legally occupy."""
    zones = {z.slug: z for z in legal_zones(workload, catalog)}
    return [(p, zones[slug]) for p in legal_profiles(workload, catalog)
            for slug in p.zones if slug in zones]


def assert_legal(workload: Workload, profile: Profile, zone: Zone) -> None:
    """Raise before any API call that would place workload on profile in zone."""
    if not zone_allowed(workload, zone):
        raise ValueError(
            f"ILLEGAL RESIDENCY: {workload.id} in {zone.slug} ({zone.country}); "
            f"residency={workload.residency} allowed={workload.allowed_countries or 'any'} "
            f"forbidden={workload.forbidden_countries}")
    if not profile_allowed(workload, profile):
        raise ValueError(f"Illegal profile {profile.name} for {workload.id}")
    if zone.slug not in profile.zones:
        raise ValueError(f"{profile.name} is not offered in {zone.slug}")
