"""Run-wide quota ledger.

Limits are run-wide, not per zone: 20 VMs, 320 vCPUs, and GPU cards per family.
GPU VMs also consume vCPU quota, and stopped VMs keep every reservation until
they are deleted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, Profile


@dataclass
class QuotaLedger:
    max_vms: int
    max_vcpus: int
    gpu_family_quotas: dict[str, int]
    vms: int = 0
    vcpus: int = 0
    cards: dict[str, int] = field(default_factory=dict)

    @classmethod
    def for_catalog(cls, catalog: Catalog) -> "QuotaLedger":
        return cls(catalog.max_vms, catalog.max_vcpus, dict(catalog.gpu_family_quotas))

    def violation(self, profile: Profile, count: int = 1) -> str | None:
        """Why adding count VMs of this profile would breach quota, else None."""
        if self.vms + count > self.max_vms:
            return f"VM quota {self.vms + count}/{self.max_vms}"
        if self.vcpus + profile.cpu_cores * count > self.max_vcpus:
            return f"vCPU quota {self.vcpus + profile.cpu_cores * count}/{self.max_vcpus}"
        if profile.gpu_count:
            limit = self.gpu_family_quotas.get(profile.family)
            if limit is None:
                return f"unsupported GPU family {profile.family}"
            used = self.cards.get(profile.family, 0) + profile.gpu_count * count
            if used > limit:
                return f"{profile.family} card quota {used}/{limit}"
        return None

    def fits(self, profile: Profile, count: int = 1) -> bool:
        return self.violation(profile, count) is None

    def add(self, profile: Profile, count: int = 1) -> None:
        breach = self.violation(profile, count)
        if breach:
            raise ValueError(f"Quota breach: {breach}")
        self.vms += count
        self.vcpus += profile.cpu_cores * count
        if profile.gpu_count:
            self.cards[profile.family] = self.cards.get(profile.family, 0) + profile.gpu_count * count

    def remove(self, profile: Profile, count: int = 1) -> None:
        self.vms -= count
        self.vcpus -= profile.cpu_cores * count
        if profile.gpu_count:
            self.cards[profile.family] = self.cards.get(profile.family, 0) - profile.gpu_count * count

    def summary(self) -> str:
        cards = ", ".join(f"{f}={n}/{self.gpu_family_quotas[f]}"
                          for f, n in sorted(self.cards.items()) if n)
        return (f"vms={self.vms}/{self.max_vms} vcpus={self.vcpus}/{self.max_vcpus}"
                + (f" cards[{cards}]" if cards else ""))
