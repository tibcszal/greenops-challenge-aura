# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A fork of the Exoscale **GreenOps Challenge starter kit** plus `controller/`, our own
competition orchestrator. The orchestrator runs on the developer's machine and drives a
hosted simulator over HTTP APIs: it decides *when and where* 15 workloads run inside a
120 simulated-hour window, and is scored on electricity-usage emissions
(`greenops-score-v2`, `impact_basis: electricity_usage`).

Two code families coexist, do not mix them:

- `examples/` + `scripts/` + `tests/test_work.py` + `tests/test_public_and_routing.py`:
  upstream starter demos (Python, Go, Terraform) that place **2 of 15** workloads. Reference
  material and guardrail tests; avoid rewriting unless upstream behavior is the subject.
- `controller/` + `tests/test_controller.py`: our solution. All new scheduling logic goes here.
  `controller/` is still untracked in git.

## Commands

```sh
# Setup (native). Python 3.10+, Go 1.25+.
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt

# Full gate (Go + Python). Needs .venv on PATH.
PATH="$PWD/.venv/bin:$PATH" make test

# Python tests only / one class / one test
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_controller.FullPackPlan -v
.venv/bin/python -m unittest tests.test_controller.Legality.test_eu_residency_excludes_switzerland

# Controller, offline (no credentials needed)
.venv/bin/python -m controller plan                 # allocation table + peak quota + kWh estimate
.venv/bin/python -m controller plan --json          # static-plan.json shaped placements
.venv/bin/python -m controller validate             # non-zero exit unless whole pack is legal + feasible

# Controller, live (needs greenops-exo.json for the run)
.venv/bin/python -m controller status
.venv/bin/python -m controller run --state .local/<run>-receipt.json
.venv/bin/python -m controller destroy --state .local/<run>-receipt.json
GREENOPS_GRID_SECRET=... .venv/bin/python -m controller grid --area DE --horizon 24

# Toolchain container (exo CLI, Terraform, Go, Python preinstalled); checkout at /workspace
docker run --rm -it -v "$PWD:/workspace" ghcr.io/exogmi/greenops-challenge-starter-kit:latest
make image                                          # build that image locally
```

`make test` must be green before calling work done; CI runs the same target plus a
multi-arch image build.

## Controller architecture

Layered bottom-up, each module single-purpose:

- `catalog.py`: typed, frozen view of `challenge/catalog.json` (11 zones, 22 profiles,
  15 workloads, run quotas). Public metadata only; `Decimal` everywhere money-like.
- `legality.py`: residency and profile rules. `assert_legal()` is the **only** gate that may
  authorize a placement, and every create path must call it.
- `sizing.py`: runtime/energy estimates. `effective_throughput()` holds the single
  multi-worker scaling assumption (pessimistic `n * (1 - sync_overhead * (n-1))`), so it is
  the one place to correct when observed progress disagrees.
- `quota.py`: `QuotaLedger`, the live run-wide counter (VMs, vCPUs, GPU cards per family).
- `timeline.py`: feasibility is *peak concurrent* quota over the 120h window, sampled on a
  900 s grid, not the sum of assignments. Supports `reserve_vms`/`reserve_vcpus` holdback.
- `planner.py`: searches the lowest utilization cap (0.30 to 0.98) that places the whole pack
  under quota, then `_polish()` spends leftover quota on the tightest workload. Produces
  `Plan` of `Assignment`s. **Currently carbon-blind**; `candidates()` and the `Plan` shape are
  the intended reuse points for an electricity-aware planner.
- `endpoints.py`, `signing.py`: run scoping and EXO2-HMAC-SHA256. Signature covers the
  complete prefixed path including `/runs/{id}/...`.
- `cloudapi.py`: zone discovery + VM lifecycle over the official `exoscale` binding, with
  `RunOnlyAdapter` refusing any request outside the configured run prefix.
- `workapi.py`: read-only `GET /work/v1/status`, the authoritative view of virtual time, run
  state, qualification and per-workload progress. Handles 429 by returning `None`.
- `gridapi.py`: this run's electricity API (carbon intensity latest/forecast/history,
  renewable level). Read-only, not yet wired into planning.
- `state.py`: durable receipt (`.local/controller-state.json`, mode 0600) of VMs we created,
  keyed to one run's endpoint; cleanup only touches these rows.
- `loop.py`: `Controller.supervise()` reconciles (`adopt_existing`), admits VMs just before
  release, polls once per wall second, deletes a workload's VMs on
  `final_output_delivered`, then `destroy_all()`.

Flow: `cli.py` builds `Catalog` then `Plan` (offline), and for live commands adds
`Account` + `Cloud` + `State` and hands them to `Controller`.

## Invariants that must not regress

- **Residency**: one invalid placement, even a rejected one, permanently disqualifies the
  run. `legality.assert_legal()` before any create, no exceptions. Switzerland is not EU.
- **Run scoping**: every request stays inside `/runs/{id}/`. Never synthesize Exoscale
  production hostnames, never strip the run prefix, never read another run's data. The
  endpoint checks and `RunOnlyAdapter` exist to make a misconfiguration fail loudly.
- **Electricity information rules** (`docs/RULES.md`): decisions may use only data published
  by *this* run's grid endpoint at the current simulated time. Do not embed, cache across
  runs, or reconstruct future electricity values; no external feeds or historical actuals.
  Keep electricity series out of the repo and out of any image.
- **The plan is advisory, the ledger is authoritative**: real runtimes drift, so `loop.py`
  defers on live quota breach rather than failing the run.
- **Delivery, not progress**: a workload is done at `final_output_delivered: true`, not 100%
  compute. VMs keep emitting until deleted; stop retains quota, delete discards
  uncheckpointed work (fatal for `restart_from_zero` jobs).
- **Deterministic VM names** (`<workload-id>-<i>`) make a lost create response recoverable
  instead of duplicated. Inspect before retrying an ambiguous create.
- One receipt (`--state`) per run; a receipt refuses a different run's endpoint. Never run
  two controllers or an example against the same run.

## Conventions

- Stdlib only in `controller/` apart from `requests` and the `exoscale` binding
  (`requirements.in` pins `exoscale==0.16.3`; `requirements.txt` is hash-locked, Go uses
  `go.sum`). Dependency additions need a hash-locked refresh.
- `Decimal` for work, watts, PUE and utilization; floats only for display.
- Frozen dataclasses for parsed records, `parse()` classmethods at the boundary.
- Module docstrings carry the *why* and the competition constraint; keep that habit.
- Credentials live in `greenops-exo.json` (gitignored, one per run); mutable output under
  `.local/`. Judging containers get 2 CPUs, 1 GiB RAM, a 15-minute wall limit and only
  `/tmp` writable, so keep state paths configurable.

## Reference docs

`docs/API.md` (cloud, work-status, electricity endpoints, PUE table),
`docs/RULES.md` (scoring, prohibited future knowledge, judging),
`docs/DEVELOPMENT.md` (native setup, second attempt, live smoke, publication),
`challenge/schemas/` (JSON schemas), `challenge/manifest.json` (scenario id + asset hashes).
