# Carbon-aware orchestrator

Controls all 15 workloads of the GreenOps challenge.  Code: `core.py` (pure decisions),
`controller.py` (loop, quota handling, VM lifecycle), `cloud.py` (Exoscale binding + grid API),
`run.py` (CLI).  Tests: `tests/test_orchestrator.py` with an offline stand-in simulator
(`tests/fake_env.py`, synthetic electricity - never real or run data).

## Run (inside the starter container, fresh run credentials in `greenops-exo.json`)

```sh
export GREENOPS_GRID_ENDPOINT='…/runs/<id>/grid/v4'   # from "API credentials" of THIS run
export GREENOPS_GRID_SECRET='…'
python -m orchestrator.run run --config greenops-exo.json
python -m orchestrator.run cleanup --config greenops-exo.json   # delete leftovers after a crash
python -m orchestrator.run plan                                  # offline: legal options per workload
python -m orchestrator.run run --no-carbon                       # baseline for your comparison
```

## Policy

1. **Legality first**: every placement is checked against country allow/deny lists, EU/CH/DACH
   residency, allowed profiles/families and GPU memory, twice (ranking and right before `create`).
2. **Placement**: cheapest legal (zone, profile) = PUE x buyable intensity x energy of the
   remaining work, where buyable intensity is the mean of the cleanest forecast hours the plan
   can actually fit into, not the window's flat mean (a plan that is paused during dirty hours
   never pays the mean, and a shorter plan fits inside a deeper dip); prefers plans that use
   <=55 % of the window (room to pause), smallest VM on near-ties (<=3 %), because quota is shared.
3. **Timing**: a VM runs in the cleanest `q` share of the next 24 h, `q` = needed time / time left
   x margin. Stopped VMs draw no power but keep quota, so pausing is switched off while
   unadmitted workloads would not fit into the quotas ("pressure"). Little slack => run non-stop,
   scale out to more workers if behind schedule.
4. **Quota reservation**: workloads with less slack that release soon keep room reserved.
5. **Cleanup**: VMs are deleted when `final_output_delivered` is true; a tiny unbound VM starts the
   clock and is deleted immediately so real workloads can wait for a clean hour.

Only this run's published forecast is used; nothing is cached across runs or embedded.
