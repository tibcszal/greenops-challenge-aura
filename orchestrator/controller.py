"""Carbon-aware controller: decides when, where and on what each workload runs.

Policy (all inputs come from the controlled run: public specs, the run's own
work status and its currently published electricity forecast):

* Placement  - for every workload pick the legal (zone, profile) with the
  lowest expected carbon for the remaining work, preferring plans that leave
  slack for pausing and small footprints (shared vCPU/GPU quota).
* Timing     - a VM only exists/runs during the cleanest hours of the next
  day.  The run-share q is derived from the slack to the deadline; stopped VMs
  draw no power but keep their quota.  Jobs with little slack run
  continuously, and are scaled out to more workers if they fall behind.
* Cleanup    - VMs are deleted as soon as the final output is delivered.
"""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from pathlib import Path

from .core import (HOUR, Illegal, Model, Usage, decide_run, eff_rate, over_quota,
                   min_footprint, parse_ts, pick, rank_options, rate_per_hour, workers_needed)

END_STATES = {"COMPLETED", "INVALIDATED", "UNAVAILABLE"}
PRESSURE_H = 8.0        # only workloads releasing within this many hours create quota pressure
RESERVE_H = 36.0       # reserve quota for urgent workloads released within this many hours
DWELL_H = 0.9          # minimum virtual hours between start/stop toggles
NAME = re.compile(r"^orch-(?P<wid>w-[a-z0-9-]+?)-(?P<n>\d+)-p(?P<pid>\d+)$")


class Controller:
    # carbon_aware is not a run option: the CLI always schedules on the forecast and refuses to
    # start without grid credentials.  It stays here only so the offline tests can measure the
    # uncontrolled baseline the optimizer is supposed to beat.
    def __init__(self, env, model: Model, *, carbon_aware=True, margin=1.4,
                 exclude=(), state_path=None, log=print):
        self.env, self.m = env, model
        self.carbon_aware, self.margin = carbon_aware, margin
        self.exclude, self.log = set(exclude), log
        self.state_path = Path(state_path) if state_path else None
        self.vms: dict[str, list[dict]] = {}
        self.starter: dict | None = None
        self.done: set[str] = set()
        self.last_change: dict = {}
        self.series: dict[str, list[float]] = {}
        self.ci_mean: dict[str, float] = {}
        self._grid_hour = None
        self.pressure = False
        self._load()

    # ---------------------------------------------------------------- state
    def _load(self):
        if self.state_path and self.state_path.exists():
            d = json.loads(self.state_path.read_text())
            self.vms, self.starter = d.get("vms", {}), d.get("starter")

    def _save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"vms": self.vms, "starter": self.starter}, indent=1))
        tmp.replace(self.state_path)

    def adopt(self):
        """Recover VMs created by an earlier invocation (deterministic names)."""
        known = {v["id"] for vs in self.vms.values() for v in vs}
        for row in self.env.adopt():
            mt = NAME.match(row["name"])
            if row["id"] in known:
                continue
            if row["name"] == "orch-starter":
                self.starter = {"id": row["id"], "zone": row["zone"], "run": True, "pid": None}
            elif mt:
                pid = next((i for i in self.m.profiles if i.endswith(mt["pid"].zfill(2))), None)
                self.vms.setdefault(mt["wid"], []).append(
                    {"id": row["id"], "zone": row["zone"], "pid": pid, "name": row["name"], "run": True})
        self._save()

    def usage(self) -> Usage:
        u = Usage()
        for vs in self.vms.values():
            for v in vs:
                u.add(self.m.profiles[v["pid"]])
        if self.starter and self.starter.get("pid"):
            u.add(self.m.profiles[self.starter["pid"]])
        return u

    # ----------------------------------------------------------------- grid
    def _refresh_grid(self, now):
        hour = now.replace(minute=0, second=0, microsecond=0)
        if not self.carbon_aware or hour == self._grid_hour:
            return
        self._grid_hour = hour
        areas = sorted({z["electricity_area"] for z in self.m.zones.values()})
        batch = getattr(self.env, "forecasts", None)
        fs = batch(areas) if batch else {a: self.env.forecast(a) for a in areas}
        for area in areas:
            f = fs.get(area)
            if f is None:
                continue
            vals = [ci for dt, ci in f if dt >= hour]
            if vals:
                self.series[area] = vals
                self.ci_mean[area] = sum(vals[:24]) / len(vals[:24])
        if self.ci_mean:
            avg = sum(self.ci_mean.values()) / len(self.ci_mean)
            for z in self.m.zones.values():
                self.ci_mean.setdefault(z["electricity_area"], avg)

    # ----------------------------------------------------------- clock start
    def _start_clock(self):
        """The first accepted VM starts the clock.  Use a tiny unbound VM and
        delete it right away, so real workloads can wait for a clean hour."""
        if self.starter:
            return
        p = min((p for p in self.m.profiles.values() if p["gpu_count"] == 0),
                key=lambda p: float(p["active_watts"]))
        zone = min((z for z in p["zones"] if z not in self.exclude), key=lambda z: float(self.m.zones[z]["pue"]))
        vid = self.env.create(zone, p, None, "orch-starter")
        self.starter = {"id": vid, "zone": zone, "pid": p["id"], "run": True}
        self._save()
        self.log(f"clock starter VM {vid} in {zone}")

    # ----------------------------------------------------------------- tick
    def tick(self) -> str:
        st = self.env.status()
        if st is None:
            return "wait"
        if st["qualification"] == "DISQUALIFIED":
            raise RuntimeError("run is disqualified - stopping")
        if st["state"] in END_STATES:
            n = sum(1 for w in st["workloads"] if w["final_output_delivered"])
            self.log(f"run is {st['state']} at {st['virtual_time']} with {n} of {len(st['workloads'])} "
                     "workloads delivered - nothing left to control. Create a fresh run for a new attempt.")
            self.cleanup_all()
            return "ended"
        if st["state"] == "READY":
            self._start_clock()
            return "wait"
        if st["state"] == "PAUSED":
            return "wait"
        now = parse_ts(st["virtual_time"])
        if self.starter:
            self.env.delete(self.starter["zone"], self.starter["id"])
            self.starter = None
            self._save()
        sw = {w["id"]: w for w in st["workloads"]}
        assign = {a["vm_id"]: a["state"] for w in st["workloads"] for a in w.get("assignments", [])}
        for wid, s in sw.items():
            if s["final_output_delivered"] and wid not in self.done:
                self._delete_workload(wid)
                self.done.add(wid)
        self._refresh_grid(now)
        todo = [wid for wid in self.m.workloads if wid not in self.done and wid in sw]
        plans = {wid: self._assess(self.m.workloads[wid], sw[wid], now) for wid in todo}
        self.pressure = self._pressure(todo, plans, now)
        self._todo, self._plans = todo, plans
        for wid in sorted(todo, key=lambda i: plans[i]["laxity"]):
            self._act(self.m.workloads[wid], sw[wid], plans[wid], now, assign)
        return "done" if len(self.done) == len(self.m.workloads) else "wait"

    # ------------------------------------------------------------ assessment
    def _assess(self, w, s, now):
        completed, required = float(s["completed_work"]), float(w["required_work"])
        vms = self.vms.get(w["id"], [])
        to_dl = (parse_ts(w["deadline_at"]) - now).total_seconds() / HOUR
        out_h = w["output_seconds"] / HOUR
        staged = completed > 0
        plan = None
        if vms:
            p = self.m.profiles[vms[0]["pid"]]
            rate = eff_rate(p, len(vms), float(w["sync_overhead"]))
            zone = vms[0]["zone"]
        else:
            opts = rank_options(self.m, w, now, completed, self.ci_mean, self.exclude)
            plan = pick(opts)
            if plan is None:
                return {"laxity": -1e9, "plan": None}
            p = self.m.profiles[plan.pid]
            rate, zone = rate_per_hour(p), plan.zone
        rem_h = (required - completed) / rate
        if not staged:
            rem_h += (w["input_seconds"] + float(p["boot_seconds"])) / HOUR
        return {"laxity": to_dl - out_h - rem_h, "plan": plan, "p": p, "zone": zone,
                "rem_h": rem_h, "to_dl": to_dl, "out_h": out_h, "completed": completed}

    def _pressure(self, todo, plans, now) -> bool:
        """True while the VMs we hold plus the VMs the still-unadmitted workloads
        need would exceed the quotas.  Stopped VMs keep their quota, so pausing
        under pressure would only delay the workloads waiting for capacity."""
        u = self.usage()
        for wid in todo:
            a = plans[wid]
            rel = parse_ts(self.m.workloads[wid]["release_at"])
            if (not self.vms.get(wid) and a["plan"] is not None
                    and (rel - now).total_seconds() < PRESSURE_H * HOUR):
                u.add(a["p"])
        return over_quota(self.m, u)

    # ---------------------------------------------------------------- action
    def _act(self, w, s, a, now, assign):
        wid = w["id"]
        completed, required = a.get("completed", 0.0), float(w["required_work"])
        vms = self.vms.get(wid, [])
        if completed >= required - 1e-6:        # output phase: never pause
            self._power(wid, True, now, assign, force=True)
            return
        if a["plan"] is None and not vms:
            self.log(f"{wid}: no legal option")
            return
        if not vms and now < parse_ts(w["release_at"]):
            return
        series = self.series.get(self.m.zones[a["zone"]]["electricity_area"], []) if self.carbon_aware else []
        run, forced = decide_run(a["rem_h"], a["to_dl"], a["out_h"], series, self.margin)
        if self.pressure:
            run = True
        if not vms:
            if run:
                self._create(w, a["plan"], now)
            return
        if forced:
            self._scale_out(w, a, now)
        else:
            prev = vms[0]["run"]
            if run != prev and (now - self.last_change.get(wid, now - timedelta(days=9))).total_seconds() < DWELL_H * HOUR:
                run = prev
        self._power(wid, run, now, assign, force=forced)

    def _create(self, w, plan_opt, now):
        wid = w["id"]
        usage = self._with_reservations(wid, now)
        opts = rank_options(self.m, w, now, 0.0, self.ci_mean, self.exclude)
        o = pick(opts, self.m, usage)
        if o is None:
            return                              # wait for quota
        self._spawn(w, o.zone, o.pid)
        self.last_change[wid] = now

    def _with_reservations(self, wid, now) -> Usage:
        """Current usage plus room kept free for workloads that must not be blocked:
        more urgent ones, and single-worker jobs that cannot scale out and have a tight window."""
        usage = self.usage()
        mine = self._plans[wid]["laxity"]
        for other in self._todo:
            a = self._plans[other]
            wo = self.m.workloads[other]
            if other == wid or self.vms.get(other) or a["plan"] is None:
                continue
            if (parse_ts(wo["release_at"]) - now).total_seconds() >= RESERVE_H * HOUR:
                continue
            opts = rank_options(self.m, wo, now, 0.0, self.ci_mean, self.exclude)
            tight = int(wo["max_workers"]) == 1 and min(o.ratio for o in opts) > 0.4
            if a["laxity"] < mine or tight:
                o = min_footprint(opts)
                if o:
                    usage.add(self.m.profiles[o.pid])
        return usage

    def _spawn(self, w, zone, pid):
        self.m.assert_legal(w, zone, pid)       # hard stop before any API call
        n = len(self.vms.get(w["id"], []))
        name = f"orch-{w['id']}-{n}-p{pid[-2:]}"
        p = self.m.profiles[pid]
        vid = self.env.create(zone, p, w["id"], name)
        self.vms.setdefault(w["id"], []).append({"id": vid, "zone": zone, "pid": pid, "name": name, "run": True})
        self._save()
        self.log(f"{w['id']}: +VM {name} in {zone}")

    def _scale_out(self, w, a, now):
        wid, vms = w["id"], self.vms[w["id"]]
        if len(vms) >= int(w["max_workers"]):
            return
        remaining = float(w["required_work"]) - a["completed"]
        avail = a["to_dl"] - a["out_h"] - 1.0 - (w["input_seconds"] + 120) / HOUR
        want = workers_needed(remaining, avail, a["p"], w)
        usage = self._with_reservations(wid, now)
        while len(self.vms[wid]) < want and usage.fits(self.m, a["p"]):
            self._spawn(w, vms[0]["zone"], vms[0]["pid"])
            usage.add(a["p"])

    def _power(self, wid, run, now, assign, force=False):
        changed = False
        for v in self.vms.get(wid, []):
            actual = assign.get(v["id"], "running" if v["run"] else "stopped")
            if actual == "creating":
                continue
            if run and actual == "stopped":
                self.env.set_power(v["zone"], v["id"], True)
                changed = True
            elif not run and actual == "running":
                self.env.set_power(v["zone"], v["id"], False)
                changed = True
            v["run"] = run
        if changed:
            self.last_change[wid] = now
            self._save()
            self.log(f"{wid}: {'resume' if run else 'pause'} at {now:%d %H:%M} virtual")

    # --------------------------------------------------------------- cleanup
    def _delete_workload(self, wid):
        for v in self.vms.pop(wid, []):
            try:
                self.env.delete(v["zone"], v["id"])
                self.log(f"{wid}: deleted {v['name']}")
            except Exception as e:                      # e.g. 409 once the run has finished
                self.log(f"{wid}: delete of {v['name']} failed: {e!r}")
        self._save()

    def cleanup_all(self):
        for wid in list(self.vms):
            self._delete_workload(wid)
        if self.starter:
            try:
                self.env.delete(self.starter["zone"], self.starter["id"])
            except Exception as e:
                self.log(f"starter delete failed: {e!r}")
            self.starter = None
            self._save()


def run_loop(ctl: Controller, poll=1.0, log=print):
    ctl.adopt()
    errors = 0
    while True:
        started = time.monotonic()
        try:
            r = ctl.tick()
            if r == "done":
                log("all workloads delivered and VMs deleted")
                return
            if r == "ended":
                return
            errors = 0
        except Illegal:
            raise
        except Exception as e:                  # transient API/network problems
            errors += 1
            log(f"tick failed ({errors}): {e!r}")
            if errors > 30:
                raise
        # Pace from the start of the attempt, not the end of it.  A slow or stalled call has
        # already spent the interval (and 12 virtual minutes per wall second with it), so
        # sleeping another full `poll` on top only widens the blind gap.  The subtraction still
        # keeps a floor between attempts, so an instantly failing call cannot spin hot.
        time.sleep(max(0.0, poll - (time.monotonic() - started)))
