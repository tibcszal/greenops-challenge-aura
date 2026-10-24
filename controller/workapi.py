"""Read-only work telemetry.

GET /runs/{id}/work/v1/status is the single authoritative view of virtual time,
run state, qualification and per-workload progress. It takes no parameters and
cannot advance the clock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import requests

from .endpoints import Account
from .signing import authorization

POLL_TIMEOUT_SECONDS = 10
MAX_BACKOFF_SECONDS = 10
TERMINAL_STATES = {"COMPLETED", "INVALIDATED", "UNAVAILABLE"}


@dataclass(frozen=True)
class WorkloadStatus:
    id: str
    state: str
    required_work: str
    completed_work: str
    lost_work: str
    final_output_delivered: bool
    assignments: tuple[dict, ...]

    @property
    def progress(self) -> float:
        required = float(self.required_work)
        return float(self.completed_work) / required if required else 0.0

    @property
    def vm_ids(self) -> set[str]:
        return {a["vm_id"] for a in self.assignments}


@dataclass(frozen=True)
class Status:
    run_id: str
    virtual_time: datetime
    speed_multiplier: float
    state: str
    qualification: str
    workloads: tuple[WorkloadStatus, ...]

    @classmethod
    def parse(cls, raw: dict) -> "Status":
        return cls(
            raw["run_id"],
            datetime.fromisoformat(raw["virtual_time"].replace("Z", "+00:00")),
            float(raw.get("speed_multiplier") or 0),
            raw["state"], raw.get("qualification", "UNKNOWN"),
            tuple(WorkloadStatus(
                w["id"], w["state"], w["required_work"], w["completed_work"],
                w.get("lost_work", "0"), bool(w["final_output_delivered"]),
                tuple(w.get("assignments") or ())) for w in raw["workloads"]))

    def workload(self, workload_id: str) -> WorkloadStatus:
        return next(w for w in self.workloads if w.id == workload_id)

    @property
    def delivered(self) -> set[str]:
        return {w.id for w in self.workloads if w.final_output_delivered}

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def fetch(account: Account, session: requests.Session | None = None) -> Status | None:
    """One status poll. Returns None after a rate-limit backoff, so callers retry."""
    url = account.work_endpoint + "/status"
    http = session or requests
    response = http.get(
        url,
        headers={"Authorization": authorization(account.key, account.secret, "GET", url)},
        timeout=POLL_TIMEOUT_SECONDS, allow_redirects=False)
    if response.status_code == 429:
        delay = min(MAX_BACKOFF_SECONDS, max(1, int(response.headers.get("Retry-After", "1"))))
        time.sleep(delay)
        return None
    response.raise_for_status()
    return Status.parse(response.json())
