"""Durable receipt of VMs this controller created.

The cloud API does not promise durable idempotent create receipts, so the
controller writes one locally after every admitted create. Cleanup is scoped to
these rows: it never deletes a VM it does not own. A receipt belongs to exactly
one run.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PATH = Path(".local/controller-state.json")
FILE_MODE = 0o600


@dataclass
class VMRecord:
    id: str
    name: str
    zone: str
    workload_id: str
    profile_id: str


class State:
    def __init__(self, path: Path, endpoint: str):
        self.path = Path(path)
        self.endpoint = endpoint
        self.instances: list[VMRecord] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            if raw["endpoint"] != endpoint:
                raise ValueError(
                    f"{self.path} belongs to another run; use a separate --state path")
            self.instances = [VMRecord(**row) for row in raw["instances"]]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"endpoint": self.endpoint,
                   "instances": [asdict(vm) for vm in self.instances]}
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w", opener=lambda p, flags: os.open(p, flags, FILE_MODE)) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temp.replace(self.path)

    def add(self, record: VMRecord) -> None:
        if not any(vm.id == record.id for vm in self.instances):
            self.instances.append(record)
            self.save()

    def remove(self, instance_id: str) -> None:
        self.instances = [vm for vm in self.instances if vm.id != instance_id]
        self.save()

    def for_workload(self, workload_id: str) -> list[VMRecord]:
        return [vm for vm in self.instances if vm.workload_id == workload_id]

    def by_name(self, name: str) -> VMRecord | None:
        return next((vm for vm in self.instances if vm.name == name), None)
