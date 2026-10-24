# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A fork of the Exoscale **GreenOps Challenge starter kit** plus `orchestrator/`, our own
competition solution. It runs on the developer's machine (or as a submission image) and drives
a hosted simulator over HTTP APIs: it decides *when, where and on what* each of the 15
workloads runs inside a 120 simulated-hour window, and is scored on electricity-usage
emissions (`greenops-score-v2`, `impact_basis: electricity_usage`, scenario `pack-40218962a6`).

Three code families coexist:

- `orchestrator/` + `tests/test_orchestrator.py` + `tests/fake_env.py`: our solution. All new
  scheduling logic goes here. Untracked in git until committed, like `CLAUDE.md` itself.
- `examples/python/static.py` and `examples/python/work.py`: upstream starter helpers that the
  orchestrator **imports at runtime** (`sys.path.insert` of `examples/python`): endpoint checks,
  zone discovery, signed client, signed work-status call with 429 backoff. Load-bearing, not
  just reference. Changing them changes the live run.
- rest of `examples/` + `scripts/`: upstream demos (Python static plan, Go, Terraform) that place
  2 of 15 workloads. Reference material plus the only Go tests in `make test`; avoid rewriting
  unless upstream behavior is the subject.

## Commands

```sh
# Setup (native). Python 3.10+, Go 1.25+.
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt

# Full gate (Go + Python). Needs .venv on PATH.
PATH="$PWD/.venv/bin:$PATH" make test

# Python tests only / one class / one test
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_orchestrator.EndToEnd -v
.venv/bin/python -m unittest tests.test_orchestrator.LegalityTests.test_known_restrictions

# Orchestrator, offline (no credentials needed): legal options + chosen plan per workload
.venv/bin/python -m orchestrator.run plan
.venv/bin/python -m orchestrator.run plan --avoid-zones fr-par-1

# Orchestrator, live (needs greenops-exo.json plus greenops-grid.json: {"secret": "..."};
# the grid endpoint is derived from the cloud one, GREENOPS_GRID_SECRET/_ENDPOINT override)
.venv/bin/python -m orchestrator.run run --config greenops-exo.json
.venv/bin/python -m orchestrator.run run --margin 1.6           # higher = pause less
.venv/bin/python -m orchestrator.run cleanup --config greenops-exo.json   # delete leftovers after a crash

# Submission image (starter toolchain + orchestrator, entrypoint `orchestrator.run run`)
docker build -f Dockerfile.orchestrator -t greenops-orchestrator:local .

# Toolchain container (exo CLI, Terraform, Go, Python preinstalled); checkout at /workspace
docker run --rm -it -v "$PWD:/workspace" ghcr.io/exogmi/greenops-challenge-starter-kit:latest
make image                                          # build that image locally
```

`make test` must be green before calling work done (currently 10 Python tests plus the Go
example tests); CI runs the same target plus `make archive` and a multi-arch image build.

## Orchestrator architecture

Four modules, split by what they are allowed to touch:

- `core.py`: pure decisions. No network, no wall clock, everything a function of public
  catalog/workload data, the run's *current* virtual time and its *currently published*
  forecast. Holds `Model` (zones, profiles, workloads, quotas) with the legality predicates
  `zone_ok` / `profile_ok` / `assert_legal`, `Usage` (live VM / vCPU / GPU-card counter with
  `fits` and `over_quota`), `rank_options` + `pick` (carbon cost = PUE x mean forecast
  intensity x active watts x work hours, chosen within feasibility `TIERS` 0.55 / 0.85 / 0.97
  of the window, near-ties within 3 % go to the smallest footprint), `decide_run` (run only in
  the cleanest `q` quantile of the forecast, `q` = needed time / time left x margin),
  `workers_needed` and `eff_rate` (the single multi-worker scaling assumption,
  `n * rate / (1 + sync_overhead * (n - 1))`, so the one place to correct when observed
  progress disagrees).
- `controller.py`: the loop and all mutation. `tick()` reads work status, deletes delivered
  workloads' VMs, refreshes the forecast once per virtual hour, assesses every workload
  (`laxity` = hours to deadline minus output minus remaining compute) and acts in ascending
  laxity order: create, scale out, pause, resume, delete. `run_loop()` polls once per wall
  second and tolerates up to 30 consecutive transient errors.
- `cloud.py`: `CloudEnv`, the only module that talks to the network: zone discovery and VM
  lifecycle over the `exoscale` binding, work status, and this run's grid API (24 h
  carbon-intensity forecast, fetched in parallel per electricity area). `warm()` preloads
  instance types and templates *before* the clock starts.
- `run.py`: CLI (`plan`, `run`, `cleanup`), config loading, state path, flags.

Flow: `run.py` builds `Model` from `challenge/*.json`, then for live commands a `CloudEnv` and
a `Controller`, and hands them to `run_loop`. `tests/fake_env.py` substitutes a `FakeEnv` for
`CloudEnv` so the whole 120 h window runs offline with synthetic electricity.

## Invariants that must not regress

- **Residency**: one invalid placement, even a rejected one, permanently disqualifies the run.
  Legality is checked twice, when ranking (`Model.options`) and in `_spawn()` via
  `assert_legal()` right before the create call. Switzerland is not EU.
- **Run scoping**: every request stays inside `/runs/{id}/`. The grid endpoint must share the
  cloud endpoint's scheme, host and run prefix, `CloudEnv.__init__` exits otherwise. Never
  synthesize Exoscale production hostnames, never strip the run prefix, never read another
  run's data.
- **Electricity information rules** (`docs/RULES.md`): decisions may use only data published by
  *this* run's grid endpoint at the current simulated time. Do not embed, cache across runs, or
  reconstruct future electricity values; no external feeds or historical actuals. The synthetic
  series in `tests/fake_env.py` exist only inside that test file. Keep electricity series out of
  the repo and out of any image.
- **Quotas are run-wide and shared**: 20 VMs, 320 vCPUs, 8 cards per GPU family (4 for
  `gpurtx6000pro`). Stopped VMs keep their quota, so pausing is switched off while unadmitted
  workloads would not fit (`_pressure`), and `_with_reservations` keeps room free for more
  urgent or non-scalable workloads. `pick()` waits rather than taking a plan that cannot meet
  the deadline.
- **Delivery, not progress**: a workload is done at `final_output_delivered: true`, not at
  100 % compute. The output phase is never paused. VMs keep emitting until deleted; stop retains
  quota, delete discards uncheckpointed work (fatal for `restart_from_zero` jobs).
- **Deterministic VM names** (`orch-<workload-id>-<i>-p<profile suffix>`, plus `orch-starter`)
  make a lost create response recoverable instead of duplicated, and let `adopt()` re-attach
  after a restart. `cloud.create()` already falls back to a name lookup on error.
- **Clock start**: virtual time is frozen until the first accepted VM, so a tiny unbound
  `orch-starter` VM starts it and is deleted on the next tick, letting real workloads wait for a
  clean hour.
- **One state receipt per run**: default `/tmp/greenops-orch-<sha of endpoint>.json`, override
  with `--state` or `GREENOPS_STATE`. Never run two orchestrators or an example against the
  same run.
- **A finished run is reported, not claimed as success**: `COMPLETED` / `INVALIDATED` /
  `UNAVAILABLE` end the loop after cleanup; `DISQUALIFIED` raises.

## Conventions

- Stdlib only in `orchestrator/` apart from `requests` and the `exoscale` binding
  (`requirements.in` pins `exoscale==0.16.3`; `requirements.txt` is hash-locked, Go uses
  `go.sum`). Dependency additions need a hash-locked refresh.
- Floats, not `Decimal`, in `orchestrator/`; catalog strings are converted at the boundary.
- Frozen-ish dataclasses for value records (`Option`, `Usage`); plain dicts for catalog rows
  straight out of JSON.
- Module docstrings carry the *why* and the competition constraint; keep that habit.
- Credentials live in `greenops-exo.json` (gitignored, one per run); mutable output under
  `.local/`. Judging containers get 2 CPUs, 1 GiB RAM, a 15-minute wall limit and only `/tmp`
  writable, so keep state paths configurable.

## Reference docs

`orchestrator/README.md` (policy summary for the submission),
`docs/API.md` (cloud, work-status, electricity endpoints, PUE table),
`docs/RULES.md` (scoring, prohibited future knowledge, judging),
`docs/DEVELOPMENT.md` (native setup, second attempt, live smoke, publication),
`challenge/schemas/` (JSON schemas), `challenge/manifest.json` (scenario id + asset hashes),
`challenge/rules.json` (frozen score policy and reference energy per workload).
