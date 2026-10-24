"""Runtime and energy estimates for a candidate configuration.

The simulator owns the real work model; these are the controller's estimates.
effective_throughput() holds the only assumption about multi-worker scaling, so
it is the single place to correct once observed progress contradicts it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .catalog import VCPU_HOUR, Profile, Workload, Zone

ZERO = Decimal(0)
ONE = Decimal(1)


def effective_throughput(profile: Profile, workers: int, sync_overhead: Decimal) -> Decimal:
    """Combined throughput of `workers` identical active VMs.

    Per challenge/schemas/runtime-workload.schema.json: throughput is divided by
    1 + sync_overhead * (active_workers - 1). The divisor counts *active* workers,
    so stopping a worker also reduces the coordination penalty on the rest.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
    divisor = ONE + sync_overhead * (workers - 1)
    return profile.throughput * workers / divisor


def compute_seconds(workload: Workload, profile: Profile, workers: int) -> Decimal:
    """Seconds of computation to finish required_work on this configuration."""
    throughput = effective_throughput(profile, workers, workload.sync_overhead)
    if throughput <= ZERO:
        return Decimal("Infinity")
    units_per_second = throughput / 3600 if workload.work_unit == VCPU_HOUR else throughput
    return workload.required_work / units_per_second


@dataclass(frozen=True)
class Estimate:
    """What one candidate configuration costs and how much deadline slack it leaves."""
    workload: Workload
    profile: Profile
    zone: Zone
    workers: int
    compute_seconds: Decimal
    total_seconds: Decimal
    vcpus: int

    @property
    def slack_seconds(self) -> Decimal:
        return Decimal(self.workload.window_seconds) - self.total_seconds

    @property
    def utilization(self) -> Decimal:
        """Fraction of the release-to-deadline window the job needs end to end."""
        return self.total_seconds / Decimal(self.workload.window_seconds)

    @property
    def energy_kwh(self) -> Decimal:
        """Active-phase electricity including zone PUE; excludes idle and boot tails."""
        watts = self.profile.active_watts * self.workers
        return watts * self.compute_seconds / Decimal(3600 * 1000) * self.zone.pue

    @property
    def cards(self) -> int:
        return self.profile.gpu_count * self.workers


def estimate(workload: Workload, profile: Profile, zone: Zone, workers: int) -> Estimate:
    """End-to-end wall clock: boot, input staging, compute, output delivery."""
    compute = compute_seconds(workload, profile, workers)
    total = (Decimal(profile.boot_seconds) + Decimal(workload.input_seconds)
             + compute + Decimal(workload.output_seconds))
    return Estimate(workload, profile, zone, workers, compute, total,
                    profile.cpu_cores * workers)
