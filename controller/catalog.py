"""Typed view over the public challenge catalog.

Contains only public workload/resource metadata shipped in challenge/catalog.json.
No run-specific state and no electricity series ever belong in here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "challenge/catalog.json"

VCPU_HOUR = "vcpu_hour"
TFLOP = "tflop"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Zone:
    slug: str
    name: str
    country: str
    electricity_area: str
    eu_member: bool
    pue: Decimal

    @classmethod
    def parse(cls, raw: dict) -> "Zone":
        return cls(raw["slug"], raw["name"], raw["country"], raw["electricity_area"],
                   bool(raw["eu_member"]), Decimal(raw["pue"]))


@dataclass(frozen=True)
class Profile:
    id: str
    family: str
    size: str
    cpu_cores: int
    gpu_count: int
    memory_gib: int
    gpu_memory_gib: int | None
    throughput: Decimal
    work_unit: str
    active_watts: Decimal
    idle_watts: Decimal
    boot_seconds: int
    zones: tuple[str, ...]

    @classmethod
    def parse(cls, raw: dict) -> "Profile":
        return cls(raw["id"], raw["family"], raw["size"], int(raw["cpu_cores"]),
                   int(raw["gpu_count"]), int(raw["memory_gib"]),
                   int(raw["gpu_memory_gib"]) if "gpu_memory_gib" in raw else None,
                   Decimal(raw["throughput"]), raw["work_unit"],
                   Decimal(raw["active_watts"]), Decimal(raw["idle_watts"]),
                   int(raw["boot_seconds"]), tuple(raw["zones"]))

    @property
    def name(self) -> str:
        return f"{self.family}.{self.size}"

    @property
    def watts_per_unit(self) -> Decimal:
        """Active watts per unit of throughput; lower is more energy efficient."""
        return self.active_watts / self.throughput


@dataclass(frozen=True)
class Workload:
    id: str
    title: str
    kind: str
    required_work: Decimal
    work_unit: str
    release_at: datetime
    deadline_at: datetime
    residency: str
    allowed_countries: tuple[str, ...]
    forbidden_countries: tuple[str, ...]
    allowed_families: tuple[str, ...]
    allowed_profile_ids: tuple[str, ...]
    minimum_gpu_memory_gib: int
    max_workers: int
    sync_overhead: Decimal
    input_seconds: int
    output_seconds: int
    checkpoint_seconds: int
    restart_from_zero: bool

    @classmethod
    def parse(cls, raw: dict) -> "Workload":
        return cls(raw["id"], raw["title"], raw["kind"], Decimal(raw["required_work"]),
                   raw["work_unit"], _time(raw["release_at"]), _time(raw["deadline_at"]),
                   raw["residency"], tuple(raw.get("allowed_countries") or ()),
                   tuple(raw.get("forbidden_countries") or ()),
                   tuple(raw["allowed_families"]), tuple(raw.get("allowed_profile_ids") or ()),
                   int(raw.get("minimum_gpu_memory_gib") or 0), int(raw["max_workers"]),
                   Decimal(raw["sync_overhead"]), int(raw["input_seconds"]),
                   int(raw["output_seconds"]), int(raw["checkpoint_seconds"]),
                   bool(raw["restart_from_zero"]))

    @property
    def window_seconds(self) -> int:
        return int((self.deadline_at - self.release_at).total_seconds())


@dataclass(frozen=True)
class Catalog:
    scenario_id: str
    zones: tuple[Zone, ...]
    profiles: tuple[Profile, ...]
    workloads: tuple[Workload, ...]
    max_vms: int
    max_vcpus: int
    gpu_family_quotas: dict[str, int]

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> "Catalog":
        raw = json.loads(Path(path).read_text())
        return cls(raw["scenario_id"],
                   tuple(Zone.parse(z) for z in raw["zones"]),
                   tuple(Profile.parse(p) for p in raw["profiles"]),
                   tuple(Workload.parse(w) for w in raw["workloads"]),
                   int(raw["max_vms"]), int(raw["max_vcpus"]),
                   {k: int(v) for k, v in raw["gpu_family_quotas"].items()})

    def zone(self, slug: str) -> Zone:
        return next(z for z in self.zones if z.slug == slug)

    def profile(self, profile_id: str) -> Profile:
        return next(p for p in self.profiles if p.id == profile_id)

    def workload(self, workload_id: str) -> Workload:
        return next(w for w in self.workloads if w.id == workload_id)
