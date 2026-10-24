"""Static full-pack planner.

Phase 1 goal: one legal configuration per workload that finishes every job inside
its release-to-deadline window without breaching run-wide quota at any point in
the run. It does not look at electricity; a carbon-aware planner can reuse
candidates() and the same Plan shape.

Energy per unit of work is nearly flat across sizes within a family, so size
selection here optimizes deadline margin and vCPU footprint, not watts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import timeline
from .catalog import Catalog, Workload
from .legality import assert_legal, legal_placements
from .sizing import Estimate, estimate


@dataclass(frozen=True)
class Assignment:
    workload_id: str
    profile_id: str
    zone_slug: str
    workers: int
    estimate: Estimate

    def vm_names(self) -> list[str]:
        """Deterministic names so a lost create response can be recovered, not duplicated."""
        return [f"{self.workload_id}-{i}" for i in range(self.workers)]


@dataclass(frozen=True)
class Plan:
    scenario_id: str
    assignments: tuple[Assignment, ...]
    utilization_cap: Decimal
    peak: timeline.Peak

    def assignment(self, workload_id: str) -> Assignment:
        return next(a for a in self.assignments if a.workload_id == workload_id)

    @property
    def total_vms(self) -> int:
        return sum(a.workers for a in self.assignments)


def candidates(workload: Workload, catalog: Catalog) -> list[Estimate]:
    """Every legal configuration that can finish the workload before its deadline."""
    out = []
    for profile, zone in legal_placements(workload, catalog):
        for workers in range(1, workload.max_workers + 1):
            candidate = estimate(workload, profile, zone, workers)
            if candidate.total_seconds.is_finite() and candidate.slack_seconds > 0:
                out.append(candidate)
    return sorted(out, key=lambda e: (e.vcpus, e.workers, e.profile.watts_per_unit,
                                      e.zone.pue, e.profile.name))


def _assign(pools: dict[str, list[Estimate]], cap: Decimal) -> list[Assignment] | None:
    """Cheapest configuration per workload that stays under the utilization cap."""
    assignments = []
    for workload_id, pool in pools.items():
        chosen = next((e for e in pool if e.utilization <= cap), None)
        if chosen is None:
            return None
        assignments.append(Assignment(workload_id, chosen.profile.id, chosen.zone.slug,
                                      chosen.workers, chosen))
    return assignments


def _polish(assignments: list[Assignment], pools: dict[str, list[Estimate]],
            catalog: Catalog, horizon_hours: int, reserve_vms: int, reserve_vcpus: int,
            rounds: int = 64) -> list[Assignment]:
    """Spend leftover quota on the tightest workloads.

    The cap search treats every workload alike, so it can leave one job at 90% of
    its window while quota sits unused. Each round upgrades the current worst
    workload to its cheapest faster configuration that still passes the peak check.
    """
    current = {a.workload_id: a for a in assignments}
    for _ in range(rounds):
        worst = max(current.values(), key=lambda a: a.estimate.utilization)
        upgrades = sorted(
            (e for e in pools[worst.workload_id]
             if e.utilization < worst.estimate.utilization),
            key=lambda e: (e.vcpus, e.utilization))
        for upgrade in upgrades:
            trial = dict(current)
            trial[worst.workload_id] = Assignment(worst.workload_id, upgrade.profile.id,
                                                  upgrade.zone.slug, upgrade.workers, upgrade)
            draft = Plan(catalog.scenario_id, tuple(trial.values()),
                         Decimal(0), timeline.Peak(0, 0, {}, 0))
            if timeline.check(draft, catalog, horizon_hours,
                              reserve_vms, reserve_vcpus) is None:
                current = trial
                break
        else:
            break
    return list(current.values())


def plan(catalog: Catalog, min_cap: Decimal = Decimal("0.30"),
         max_cap: Decimal = Decimal("0.98"), step: Decimal = Decimal("0.02"),
         horizon_hours: int = 120, reserve_vms: int = 0,
         reserve_vcpus: int = 0) -> Plan:
    """Lowest utilization cap that places the whole pack within run-wide quota.

    A lower cap leaves more deadline margin, so the first feasible cap is kept.
    """
    pools = {}
    for workload in catalog.workloads:
        pool = candidates(workload, catalog)
        if not pool:
            raise ValueError(f"{workload.id} has no legal configuration meeting its deadline")
        pools[workload.id] = pool

    breach = "no cap attempted"
    cap = min_cap
    while cap <= max_cap:
        assignments = _assign(pools, cap)
        if assignments is not None:
            assignments = _polish(assignments, pools, catalog, horizon_hours,
                                  reserve_vms, reserve_vcpus)
            ordered = tuple(sorted(assignments, key=lambda a: a.workload_id))
            draft = Plan(catalog.scenario_id, ordered, cap, timeline.Peak(0, 0, {}, 0))
            breach = timeline.check(draft, catalog, horizon_hours,
                                    reserve_vms, reserve_vcpus)
            if breach is None:
                for assignment in ordered:
                    assert_legal(catalog.workload(assignment.workload_id),
                                 catalog.profile(assignment.profile_id),
                                 catalog.zone(assignment.zone_slug))
                holds = timeline.occupancies(draft, catalog)
                worst = timeline.peak(holds, horizon_hours * 3600)
                return Plan(catalog.scenario_id, ordered, cap, worst)
        cap += step
    raise ValueError(f"No quota-feasible assignment of the full workload pack: {breach}")
