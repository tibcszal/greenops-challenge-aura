#!/usr/bin/env python3
"""Carbon-aware orchestrator for the GreenOps challenge.

  python -m orchestrator.run plan                      # offline: legal options + chosen plan per workload
  python -m orchestrator.run run --config greenops-exo.json
  python -m orchestrator.run cleanup --config greenops-exo.json

Grid access (this run only): put the run's grid secret in greenops-grid.json next to the
cloud config, as {"secret": "..."}, or pass GREENOPS_GRID_SECRET. The grid endpoint is
derived from the cloud endpoint unless GREENOPS_GRID_ENDPOINT overrides it. A run without
working grid access is refused, so every run is scheduled on the same basis and stays
comparable with the others.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .controller import Controller, run_loop
from .core import Model, parse_ts, pick, rank_options

ROOT = Path(__file__).resolve().parents[1]


def load_model() -> Model:
    return Model(json.loads((ROOT / "challenge/catalog.json").read_text()),
                 json.loads((ROOT / "challenge/workloads.json").read_text()))


def grid_credentials(account: dict, config_path) -> tuple[str | None, str | None]:
    """This run's grid endpoint and secret.

    Environment first, because that is how a judging container receives them, then JSON next to
    the cloud config: `greenops-grid.json` ({"secret": ...}) or a `grid_secret` field in
    `greenops-exo.json` itself.  The endpoint is never required input: CloudEnv only accepts the
    cloud endpoint's own run prefix plus /grid/v4, so it is derived whenever it is not given.
    """
    endpoint = os.environ.get("GREENOPS_GRID_ENDPOINT")
    secret = os.environ.get("GREENOPS_GRID_SECRET")
    if not (endpoint and secret):
        blob = {}
        path = Path(config_path).with_name("greenops-grid.json")
        if path.exists():
            blob = json.loads(path.read_text())
        endpoint = endpoint or blob.get("endpoint") or account.get("grid_endpoint")
        secret = secret or blob.get("secret") or account.get("grid_secret")
    if secret and not endpoint:
        endpoint = account["endpoint"].split("/cloud/")[0] + "/grid/v4"
    return endpoint, secret


def cmd_plan(m: Model, args):
    """Offline view with flat electricity: shows the legal choices the controller has."""
    now = parse_ts("2025-09-30T00:00:00Z")
    for w in m.workloads.values():
        opts = rank_options(m, w, now, 0.0, {}, args.avoid_zones)
        o = pick(opts)
        zones = sorted({x.zone for x in opts})
        p = m.profiles[o.pid]
        print(f"{w['id']:22s} {len(opts):2d} legal options in {','.join(zones)}")
        print(f"{'':22s} -> {o.zone} {p['family']}.{p['size']} (uses {o.ratio:.0%} of its window)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["plan", "run", "cleanup"])
    ap.add_argument("--config", default=os.environ.get("GREENOPS_CONFIG", "greenops-exo.json"))
    ap.add_argument("--state", type=Path, help="receipt file (default /tmp/greenops-orch-<run>.json)")
    ap.add_argument("--margin", type=float, default=1.4, help="pause aggressiveness: higher = pause less")
    ap.add_argument("--avoid-zones", nargs="*", default=[], help="zone slugs never to use, e.g. fr-par-1")
    args = ap.parse_args(argv)
    m = load_model()
    if args.action == "plan":
        return cmd_plan(m, args)

    sys.path.insert(0, str(ROOT / "examples" / "python"))
    import static
    from .cloud import CloudEnv
    account = static.read_account(args.config)
    run_hash = hashlib.sha256(account["endpoint"].encode()).hexdigest()[:10]
    state = args.state or Path(os.environ.get("GREENOPS_STATE", f"/tmp/greenops-orch-{run_hash}.json"))
    env = CloudEnv(account, *grid_credentials(account, args.config))
    if args.action == "run" and not env.grid:
        # Never start a carbon-blind run.  A warning here is too quiet: the run still burns its
        # single 120 h window on an uncontrolled schedule, and the result then looks like a
        # regression of whatever was being tested.  Runs are comparable, so keep them all alike.
        raise SystemExit("no grid secret for this run: put it in greenops-grid.json as "
                         '{"secret": "..."}, or as a grid_secret field of the cloud config, or '
                         "in GREENOPS_GRID_SECRET. The endpoint is derived from the cloud one.")
    if args.action == "run":
        # Credentials being present is not the same as them working: forecast() swallows a 401
        # and returns None, which would leave the series empty and the schedule carbon-blind.
        # Probe once here, while the clock is still frozen, instead of finding out at the end.
        area = sorted({z["electricity_area"] for z in m.zones.values()})[0]
        if not env.forecast(area):
            raise SystemExit(f"grid endpoint returned no forecast for {area}: check "
                             "GREENOPS_GRID_SECRET and that the endpoint belongs to this run.")
    env.warm()
    ctl = Controller(env, m, margin=args.margin,
                     exclude=args.avoid_zones, state_path=state,
                     log=lambda s: print(datetime.now(timezone.utc).strftime("%H:%M:%S"), s, flush=True))
    if args.action == "cleanup":
        ctl.adopt()
        ctl.cleanup_all()
        return
    run_loop(ctl, log=print)


if __name__ == "__main__":
    main()
