#!/usr/bin/env python3
"""Carbon-aware orchestrator for the GreenOps challenge.

  python -m orchestrator.run env                       # which credentials file is in use
  python -m orchestrator.run plan                      # offline: legal options + chosen plan per workload
  python -m orchestrator.run run --config greenops-exo.json
  python -m orchestrator.run cleanup --config greenops-exo.json

Credentials for one run (key, secret, cloud/grid endpoints) come from a file:
drop the portal's download, or a `greenops.env`, anywhere from the working
directory up to the repository root and it is picked up, so one file serves the
checkout and every worktree under it.  Real environment variables still win over
the file.  `--env-file PATH` names one explicitly, `--no-env-file` ignores them.

Grid access (this run only): GREENOPS_GRID_ENDPOINT and GREENOPS_GRID_SECRET.
Without them the controller still runs, but without carbon-aware pausing/zone choice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import envfile
from .controller import MARGIN, Controller, run_loop
from .core import Model, parse_ts, pick, rank_options
from .memory import RunMemory

ROOT = Path(__file__).resolve().parents[1]


def load_model() -> Model:
    return Model(json.loads((ROOT / "challenge/catalog.json").read_text()),
                 json.loads((ROOT / "challenge/workloads.json").read_text()))


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


def cmd_env(args):
    """Report which credentials file is in use and what it set, without printing secrets."""
    if args.path:
        used = Path(args.env_file) if args.env_file else envfile.find(extra=ROOT)
        if used:
            print(used)
        return 0 if used else 1
    used, applied = envfile.load(args.env_file, extra=ROOT) if not args.no_env_file else (None, [])
    others = [p for p in envfile.candidates(extra=ROOT) if p != used]
    print(f"file:    {used}" if used else "file:    none found")
    if applied:
        print(f"applied: {', '.join(sorted(applied))}")
    for p in others:
        print(f"ignored: {p} (a nearer file wins)")
    for key, state in envfile.status():
        print(f"  {key:26s} {state}")
    gaps = envfile.missing()
    if gaps:
        sys.stdout.flush()
        print(f"missing: {', '.join(gaps)} - a live run cannot start", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["env", "plan", "run", "cleanup"])
    ap.add_argument("--env-file", default=os.environ.get("GREENOPS_ENV_FILE"),
                    help="credentials file; default: nearest greenops.env / greenops-*.env / .env "
                         "from the working directory upwards")
    ap.add_argument("--no-env-file", action="store_true",
                    help="use only real environment variables, never a file on disk")
    ap.add_argument("--path", action="store_true",
                    help="with the env action: print only the resolved file path (for scripts)")
    ap.add_argument("--config", default=os.environ.get("GREENOPS_CONFIG", "greenops-exo.json"))
    ap.add_argument("--state", type=Path, help="receipt file (default /tmp/greenops-orch-<run>.json)")
    ap.add_argument("--memory-db", type=Path, help="SQLite learning DB for this run only; default .local/greenops-memory-<run-hash>.sqlite3")
    ap.add_argument("--margin", type=float, default=MARGIN, help="pause aggressiveness: higher = pause less")
    ap.add_argument("--no-park", action="store_true",
                    help="never delete-and-restart checkpointed jobs during dirty hours (stop them instead)")
    ap.add_argument("--no-carbon", action="store_true", help="baseline: never pause, ignore forecasts")
    ap.add_argument("--avoid-zones", nargs="*", default=[], help="zone slugs never to use, e.g. fr-par-1")
    args = ap.parse_args(argv)
    if args.action == "env":
        return cmd_env(args)
    m = load_model()
    if args.action == "plan":
        return cmd_plan(m, args)

    if not args.no_env_file:
        used, applied = envfile.load(args.env_file, extra=ROOT)
        if used:
            print(f"credentials: {len(applied)} variable(s) from {used}", file=sys.stderr)
    gaps = envfile.missing()
    if gaps and not Path(args.config).exists():
        ap.error(f"missing credentials: {', '.join(gaps)}. Put this run's env file in "
                 f"{Path.cwd()} or any parent, pass --env-file PATH, or export them. "
                 "Run `python -m orchestrator.run env` to see what was found.")

    sys.path.insert(0, str(ROOT / "examples" / "python"))
    import static
    from .cloud import CloudEnv
    account = static.read_account(args.config)
    run_hash = hashlib.sha256(account["endpoint"].encode()).hexdigest()[:10]
    state = args.state or Path(os.environ.get("GREENOPS_STATE", f"/tmp/greenops-orch-{run_hash}.json"))
    memory_path = args.memory_db or Path(os.environ.get("GREENOPS_MEMORY_DB", str(ROOT / ".local" / f"greenops-memory-{run_hash}.sqlite3")))
    memory = RunMemory(memory_path, account["endpoint"])
    env = CloudEnv(account, os.environ.get("GREENOPS_GRID_ENDPOINT"), os.environ.get("GREENOPS_GRID_SECRET"))
    if not env.grid and not args.no_carbon:
        print("warning: no grid credentials -> running without carbon awareness", file=sys.stderr)
    env.warm()
    ctl = Controller(env, m, carbon_aware=bool(env.grid) and not args.no_carbon, margin=args.margin,
                     park=not args.no_park,
                     exclude=args.avoid_zones, state_path=state, memory=memory,
                     log=lambda s: print(datetime.now(timezone.utc).strftime("%H:%M:%S"), s, flush=True))
    if args.action == "cleanup":
        ctl.adopt(); ctl.cleanup_all()
        print(f"same-run SQLite records: {memory.stats()}")
        memory.close(); return
    try:
        run_loop(ctl, log=print)
    finally:
        print(f"same-run SQLite records: {memory.stats()}")
        memory.close()


if __name__ == "__main__":
    main()
