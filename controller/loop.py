"""Reactive control loop.

Phase 1 policy: admit each workload's VMs just before its release, poll work
status, and delete a workload's VMs once its final output is delivered. The
simulator owns the clock; nothing here tries to change it.

The planned allocation is advisory. The live quota ledger is authoritative, so a
workload whose quota is momentarily occupied is deferred to a later tick instead
of failing the run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from .catalog import Catalog
from .cloudapi import Cloud
from .endpoints import Account
from .planner import Plan
from .quota import QuotaLedger
from .state import State, VMRecord
from .workapi import Status, fetch

POLL_INTERVAL_SECONDS = 1.0
DEFAULT_WALL_TIMEOUT_SECONDS = 3600


@dataclass
class Event:
    at: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.at}] {self.kind:9s} {self.detail}"


class Controller:
    def __init__(self, account: Account, catalog: Catalog, plan: Plan,
                 state: State, cloud: Cloud, log=print):
        self.account = account
        self.catalog = catalog
        self.plan = plan
        self.state = state
        self.cloud = cloud
        self.log = log
        self.ledger = QuotaLedger.for_catalog(catalog)
        for record in self.state.instances:
            self.ledger.add(catalog.profile(record.profile_id))

    # -- reconciliation ----------------------------------------------------

    def adopt_existing(self) -> None:
        """Match VMs already in the run against planned names, so a restart resumes.

        Deterministic names make a create that was admitted but whose response was
        lost recoverable instead of duplicated.
        """
        planned = {name: assignment
                   for assignment in self.plan.assignments
                   for name in assignment.vm_names()}
        for instance in self.cloud.all_instances():
            assignment = planned.get(instance.name)
            if assignment is None or self.state.by_name(instance.name):
                continue
            profile = self.catalog.profile(assignment.profile_id)
            self.state.add(VMRecord(instance.id, instance.name, instance.zone,
                                    assignment.workload_id, assignment.profile_id))
            self.ledger.add(profile)
            self.emit(instance.zone, "adopt", f"{instance.name} ({instance.id})")

    def emit(self, at: str, kind: str, detail: str) -> None:
        self.log(str(Event(at, kind, detail)))

    # -- admission ---------------------------------------------------------

    def _due(self, status: Status, workload_id: str) -> bool:
        """True when the workload's release is close enough to boot its VMs now."""
        workload = self.catalog.workload(workload_id)
        profile = self.catalog.profile(self.plan.assignment(workload_id).profile_id)
        lead = timedelta(seconds=profile.boot_seconds)
        return status.virtual_time + lead >= workload.release_at

    def _pending(self, status: Status) -> list[str]:
        """Workloads with no VMs of ours and no delivered output yet."""
        return [assignment.workload_id for assignment in self.plan.assignments
                if not self.state.for_workload(assignment.workload_id)
                and assignment.workload_id not in status.delivered]

    def admit(self, status: Status) -> None:
        """Create VMs for workloads that are due and fit the live quota."""
        pending = self._pending(status)
        if not pending:
            return
        # The clock only starts on the first accepted create, so bootstrap with the
        # earliest-release workload even though its release is still ahead.
        bootstrap = not self.state.instances
        for workload_id in pending:
            if not bootstrap and not self._due(status, workload_id):
                continue
            assignment = self.plan.assignment(workload_id)
            profile = self.catalog.profile(assignment.profile_id)
            breach = self.ledger.violation(profile, assignment.workers)
            if breach:
                self.emit(status.virtual_time.isoformat(), "defer",
                          f"{workload_id} waiting on {breach}")
                continue
            self._create(status, assignment, profile)
            bootstrap = False

    def _create(self, status: Status, assignment, profile) -> None:
        from .legality import assert_legal

        workload = self.catalog.workload(assignment.workload_id)
        zone = self.catalog.zone(assignment.zone_slug)
        assert_legal(workload, profile, zone)
        client = self.cloud.zone(assignment.zone_slug)
        for name in assignment.vm_names():
            if self.state.by_name(name):
                continue
            existing = [vm for vm in client.instances() if vm.name == name]
            if len(existing) > 1:
                raise ValueError(f"Ambiguous duplicate VM name {name}; inspect this run")
            instance_id = (existing[0].id if existing else
                           client.create_instance(name, profile.family, profile.size,
                                                  workload.id))
            self.state.add(VMRecord(instance_id, name, assignment.zone_slug,
                                    workload.id, profile.id))
            self.ledger.add(profile)
            self.emit(status.virtual_time.isoformat(), "create",
                      f"{workload.id} {profile.name} {assignment.zone_slug} {name}")

    # -- cleanup -----------------------------------------------------------

    def release_delivered(self, status: Status) -> None:
        """Delete VMs of delivered workloads; idle VMs keep burning electricity."""
        for workload_id in sorted(status.delivered):
            for record in self.state.for_workload(workload_id):
                self.cloud.zone(record.zone).delete_instance(record.id)
                self.state.remove(record.id)
                self.ledger.remove(self.catalog.profile(record.profile_id))
                self.emit(status.virtual_time.isoformat(), "delete",
                          f"{workload_id} {record.name} delivered")

    def destroy_all(self) -> None:
        """Unconditional cleanup of every VM in the receipt."""
        for record in list(self.state.instances):
            self.cloud.zone(record.zone).delete_instance(record.id)
            self.state.remove(record.id)
            self.emit("cleanup", "delete", f"{record.name} ({record.id})")

    # -- main loop ---------------------------------------------------------

    def supervise(self, wall_timeout_seconds: float = DEFAULT_WALL_TIMEOUT_SECONDS) -> Status:
        """Drive the run until every workload is delivered or the run ends."""
        self.adopt_existing()
        wanted = {assignment.workload_id for assignment in self.plan.assignments}
        deadline = time.monotonic() + wall_timeout_seconds
        last_report = ""
        status = None
        while time.monotonic() < deadline:
            status = fetch(self.account)
            if status is None:
                continue
            self.release_delivered(status)
            self.admit(status)
            report = self._report(status)
            if report != last_report:
                self.emit(status.virtual_time.isoformat(), "progress", report)
                last_report = report
            if wanted <= status.delivered:
                self.emit(status.virtual_time.isoformat(), "done",
                          f"all {len(wanted)} workloads delivered; "
                          f"qualification={status.qualification}")
                self.destroy_all()
                return status
            if status.terminal:
                missing = sorted(wanted - status.delivered)
                raise ValueError(f"Run ended in {status.state}; undelivered: {missing}")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"Supervision exceeded {wall_timeout_seconds}s of wall time")

    def _report(self, status: Status) -> str:
        delivered = len(status.delivered)
        running = sum(1 for w in status.workloads if w.assignments)
        progress = sum(w.progress for w in status.workloads) / max(1, len(status.workloads))
        lost = sum(float(w.lost_work) for w in status.workloads)
        return (f"delivered={delivered}/{len(status.workloads)} active={running} "
                f"progress={progress:.1%} lost={lost:.1f} {self.ledger.summary()} "
                f"state={status.state} qual={status.qualification}")
