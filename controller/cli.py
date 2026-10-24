"""Command line entry point.

    plan      offline allocation table, no credentials needed
    validate  exit non-zero unless the full pack is legal and quota-feasible
    run       deploy, poll to delivery, clean up
    status    one work-status poll
    destroy   delete every VM in the receipt
    grid      print this run's current forecast for one electricity area
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .catalog import CATALOG_PATH, Catalog
from .legality import legal_placements
from .planner import Plan, plan as build_plan
from .state import DEFAULT_PATH, State
from . import timeline


def _hours(seconds) -> float:
    return float(seconds) / 3600


def print_plan(plan: Plan, catalog: Catalog) -> None:
    print(f"scenario {plan.scenario_id}  utilization cap {plan.utilization_cap}  "
          f"vms {plan.total_vms}")
    header = (f"{'workload':22s} {'profile':20s} {'zone':9s} {'w':>2s} {'total':>7s} "
              f"{'window':>7s} {'util':>6s} {'slack':>7s} {'vcpu':>4s} {'cards':>5s} {'kWh':>8s}")
    print(header)
    print("-" * len(header))
    for assignment in plan.assignments:
        e = assignment.estimate
        print(f"{assignment.workload_id:22s} {e.profile.name:20s} {assignment.zone_slug:9s} "
              f"{assignment.workers:2d} {_hours(e.total_seconds):6.1f}h "
              f"{e.workload.window_seconds / 3600:6.1f}h {float(e.utilization):6.1%} "
              f"{_hours(e.slack_seconds):6.1f}h {e.vcpus:4d} {e.cards:5d} "
              f"{float(e.energy_kwh):8.2f}")
    holds = timeline.occupancies(plan, catalog)
    cards = timeline.peak_cards(holds, 120 * 3600)
    peak_vms = max(sum(h.vms for h in holds if h.start <= o < h.end)
                   for o in range(0, 120 * 3600 + timeline.STEP_SECONDS, timeline.STEP_SECONDS))
    print(f"\npeak concurrent: vms {peak_vms}/{catalog.max_vms}  "
          f"vcpus {plan.peak.vcpus}/{catalog.max_vcpus}  "
          + "  ".join(f"{f} {n}/{catalog.gpu_family_quotas[f]}" for f, n in sorted(cards.items())))
    print(f"estimated active electricity: "
          f"{float(sum(a.estimate.energy_kwh for a in plan.assignments)):.1f} kWh "
          f"(active phases only, excludes boot, staging and idle tails)")


def cmd_plan(args) -> int:
    catalog = Catalog.load(args.catalog)
    plan = build_plan(catalog, reserve_vms=args.reserve_vms,
                      reserve_vcpus=args.reserve_vcpus)
    if args.json:
        print(json.dumps({
            "scenario_id": plan.scenario_id,
            "utilization_cap": str(plan.utilization_cap),
            "placements": [{
                "name": name,
                "workload_id": a.workload_id,
                "zone": a.zone_slug,
                "family": catalog.profile(a.profile_id).family,
                "size": catalog.profile(a.profile_id).size,
            } for a in plan.assignments for name in a.vm_names()],
        }, indent=2))
    else:
        print_plan(plan, catalog)
    return 0


def cmd_validate(args) -> int:
    catalog = Catalog.load(args.catalog)
    problems = []
    for workload in catalog.workloads:
        if not legal_placements(workload, catalog):
            problems.append(f"{workload.id}: no legal placement exists")
    try:
        plan = build_plan(catalog, reserve_vms=args.reserve_vms,
                          reserve_vcpus=args.reserve_vcpus)
    except ValueError as error:
        problems.append(str(error))
        plan = None
    if plan is not None:
        if len(plan.assignments) != len(catalog.workloads):
            problems.append(f"planned {len(plan.assignments)}/{len(catalog.workloads)} workloads")
        breach = timeline.check(plan, catalog, reserve_vms=args.reserve_vms,
                                reserve_vcpus=args.reserve_vcpus)
        if breach:
            problems.append(f"quota: {breach}")
        for assignment in plan.assignments:
            if assignment.estimate.slack_seconds <= 0:
                problems.append(f"{assignment.workload_id}: misses its deadline")
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    worst = max(plan.assignments, key=lambda a: a.estimate.utilization)
    print(f"OK {len(plan.assignments)}/{len(catalog.workloads)} workloads placed legally; "
          f"peak vcpus {plan.peak.vcpus}/{catalog.max_vcpus}; "
          f"tightest {worst.workload_id} at {float(worst.estimate.utilization):.1%} of its window")
    return 0


def _live(args):
    from .cloudapi import Cloud
    from .endpoints import load_account

    account = load_account(args.config)
    catalog = Catalog.load(args.catalog)
    plan = build_plan(catalog, reserve_vms=args.reserve_vms, reserve_vcpus=args.reserve_vcpus)
    state = State(Path(args.state), account.endpoint)
    return account, catalog, plan, state, Cloud(account)


def cmd_run(args) -> int:
    from .loop import Controller

    account, catalog, plan, state, cloud = _live(args)
    print_plan(plan, catalog)
    print(f"\nrun {account.run_id} receipt {args.state}\n")
    controller = Controller(account, catalog, plan, state, cloud)
    controller.supervise(wall_timeout_seconds=args.timeout)
    return 0


def cmd_status(args) -> int:
    from .endpoints import load_account
    from .workapi import fetch

    account = load_account(args.config)
    status = fetch(account)
    if status is None:
        print("rate limited; retry", file=sys.stderr)
        return 1
    print(f"{status.run_id} {status.state} qual={status.qualification} "
          f"virtual_time={status.virtual_time.isoformat()} speed={status.speed_multiplier}")
    for workload in status.workloads:
        print(f"  {workload.id:22s} {workload.state:12s} {workload.progress:6.1%} "
              f"delivered={workload.final_output_delivered!s:5s} lost={workload.lost_work} "
              f"vms={len(workload.assignments)}")
    return 0


def cmd_destroy(args) -> int:
    from .loop import Controller

    account, catalog, plan, state, cloud = _live(args)
    Controller(account, catalog, plan, state, cloud).destroy_all()
    return 0


def cmd_grid(args) -> int:
    from .endpoints import load_account
    from .gridapi import Grid

    grid = Grid(load_account(args.config))
    for point in grid.forecast(args.area, args.horizon):
        print(f"{point.datetime.isoformat()} {float(point.carbon_intensity):8.1f} gCO2e/kWh")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Shared options live only on the subparsers. Putting them on both parsers makes
    # argparse reset a value given before the subcommand back to its default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", default=str(CATALOG_PATH))
    common.add_argument("--config", default="greenops-exo.json",
                        help="downloaded greenops-exo-<run>.json")
    common.add_argument("--state", default=str(DEFAULT_PATH), help="receipt path, one per run")
    common.add_argument("--reserve-vms", type=int, default=0,
                        help="VM quota to hold back from the plan")
    common.add_argument("--reserve-vcpus", type=int, default=0,
                        help="vCPU quota to hold back from the plan")
    common.add_argument("--timeout", type=float, default=3600.0,
                        help="wall-clock limit for run, in seconds")
    parser = argparse.ArgumentParser(prog="controller", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("plan", cmd_plan), ("validate", cmd_validate), ("run", cmd_run),
                          ("status", cmd_status), ("destroy", cmd_destroy), ("grid", cmd_grid)):
        child = sub.add_parser(name, help=handler.__doc__, parents=[common])
        child.set_defaults(handler=handler)
        if name == "plan":
            child.add_argument("--json", action="store_true",
                               help="emit a static-plan.json style placement list")
        if name == "grid":
            child.add_argument("--area", default="DE", help="electricity area code, e.g. DE")
            child.add_argument("--horizon", type=int, default=24, choices=(6, 24))
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, KeyError, StopIteration, TimeoutError) as error:
        print(f"controller failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
