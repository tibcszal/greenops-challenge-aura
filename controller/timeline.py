"""Time-aware quota accounting for a planned allocation.

Run-wide quota is reused over time: a workload holds its VMs from release until
its final output is delivered, and the controller deletes them afterwards. The
feasibility question is therefore the peak concurrent usage over the run window,
not the sum of every assignment.

The plan is advisory. The live QuotaLedger stays authoritative, because real
runtimes drift from these estimates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .catalog import Catalog

STEP_SECONDS = 900


@dataclass(frozen=True)
class Occupancy:
    """One assignment's planned hold on quota, in seconds since run start."""
    workload_id: str
    start: Decimal
    end: Decimal
    vms: int
    vcpus: int
    family: str | None
    cards: int


@dataclass(frozen=True)
class Peak:
    vms: int
    vcpus: int
    cards: dict[str, int]
    at_seconds: int

    def violation(self, catalog: Catalog) -> str | None:
        if self.vms > catalog.max_vms:
            return f"peak VMs {self.vms}/{catalog.max_vms}"
        if self.vcpus > catalog.max_vcpus:
            return f"peak vCPUs {self.vcpus}/{catalog.max_vcpus}"
        for family, used in self.cards.items():
            limit = catalog.gpu_family_quotas.get(family)
            if limit is None:
                return f"unsupported GPU family {family}"
            if used > limit:
                return f"peak {family} cards {used}/{limit}"
        return None


def occupancies(plan, catalog: Catalog) -> list[Occupancy]:
    """Planned hold windows, measured from the earliest workload release."""
    origin = min(w.release_at for w in catalog.workloads)
    out = []
    for assignment in plan.assignments:
        workload = catalog.workload(assignment.workload_id)
        profile = catalog.profile(assignment.profile_id)
        start = Decimal((workload.release_at - origin).total_seconds())
        out.append(Occupancy(
            assignment.workload_id, start, start + assignment.estimate.total_seconds,
            assignment.workers, assignment.estimate.vcpus,
            profile.family if profile.gpu_count else None, assignment.estimate.cards))
    return out


def peak(holds: list[Occupancy], horizon_seconds: int) -> Peak:
    """Worst concurrent usage, sampled on a fixed grid across the run window."""
    worst = Peak(0, 0, {}, 0)
    for offset in range(0, horizon_seconds + STEP_SECONDS, STEP_SECONDS):
        live = [h for h in holds if h.start <= offset < h.end]
        vcpus = sum(h.vcpus for h in live)
        if vcpus <= worst.vcpus:
            continue
        cards: dict[str, int] = {}
        for hold in live:
            if hold.family:
                cards[hold.family] = cards.get(hold.family, 0) + hold.cards
        worst = Peak(sum(h.vms for h in live), vcpus, cards, offset)
    return worst


def peak_cards(holds: list[Occupancy], horizon_seconds: int) -> dict[str, int]:
    """Per-family card peaks, which need not coincide with the vCPU peak."""
    worst: dict[str, int] = {}
    for offset in range(0, horizon_seconds + STEP_SECONDS, STEP_SECONDS):
        for family in {h.family for h in holds if h.family}:
            used = sum(h.cards for h in holds
                       if h.family == family and h.start <= offset < h.end)
            worst[family] = max(worst.get(family, 0), used)
    return worst


def check(plan, catalog: Catalog, horizon_hours: int = 120,
          reserve_vms: int = 0, reserve_vcpus: int = 0) -> str | None:
    """Why the planned allocation breaches a run-wide quota, else None.

    Reserves hold back quota the plan may not use, leaving the live controller
    room to react when real runtimes drift from these estimates.
    """
    holds = occupancies(plan, catalog)
    horizon = horizon_hours * 3600
    worst = peak(holds, horizon)
    if worst.vcpus > catalog.max_vcpus - reserve_vcpus:
        return f"peak vCPUs {worst.vcpus}/{catalog.max_vcpus - reserve_vcpus}"
    breach = worst.violation(catalog)
    if breach:
        return breach
    for family, used in peak_cards(holds, horizon).items():
        limit = catalog.gpu_family_quotas.get(family)
        if limit is None:
            return f"unsupported GPU family {family}"
        if used > limit:
            return f"peak {family} cards {used}/{limit}"
    worst_vms = max(sum(h.vms for h in holds if h.start <= o < h.end)
                    for o in range(0, horizon + STEP_SECONDS, STEP_SECONDS))
    if worst_vms > catalog.max_vms - reserve_vms:
        return f"peak VMs {worst_vms}/{catalog.max_vms - reserve_vms}"
    return None
