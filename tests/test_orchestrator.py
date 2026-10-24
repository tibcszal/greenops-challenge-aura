import inspect
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_env import FakeEnv                      # noqa: E402
import orchestrator.controller as controller      # noqa: E402
from orchestrator.controller import Controller    # noqa: E402
from orchestrator.core import Illegal, Model, Usage, decide_run, dirty_run_h, excess, quantile  # noqa: E402
from orchestrator.run import grid_credentials   # noqa: E402

CATALOG = json.loads((ROOT / "challenge/catalog.json").read_text())
WORKLOADS = json.loads((ROOT / "challenge/workloads.json").read_text())


def model():
    return Model(CATALOG, WORKLOADS)


def run_sim(seed=1, carbon_aware=True, margin=1.25, hours=120):
    m = model()
    env = FakeEnv(m, WORKLOADS, seed)
    ctl = Controller(env, m, carbon_aware=carbon_aware, margin=margin, log=lambda *_: None)
    ticks = int(hours * 3600 / 720)
    for _ in range(ticks):
        r = ctl.tick()
        env.advance(720)
        if r == "done":
            break
    ctl.tick()
    return env, ctl


class LegalityTests(unittest.TestCase):
    def test_every_option_is_legal_for_every_workload(self):
        m = model()
        for w in WORKLOADS:
            opts = list(m.options(w))
            self.assertTrue(opts, w["id"])
            for slug, p in opts:
                m.assert_legal(w, slug, p["id"])
                c = m.zones[slug]["country"]
                self.assertNotIn(c, w.get("forbidden_countries", []))
                if w.get("allowed_countries"):
                    self.assertIn(c, w["allowed_countries"])
                if w["residency"] == "EU":
                    self.assertTrue(m.zones[slug]["eu_member"])
                if len(w["residency"]) == 2 and w["residency"] != "EU":
                    self.assertEqual(c, w["residency"])

    def test_known_restrictions(self):
        m = model()
        zones = lambda wid: {s for s, _ in m.options(m.workloads[wid])}  # noqa: E731
        self.assertEqual(zones("w-02-ch-reporting"), {"ch-dk-2", "ch-gva-2"})
        self.assertEqual(zones("w-08-ch-imaging"), {"ch-gva-2"})
        self.assertTrue(zones("w-07-eu-training") <= {"de-fra-1", "hr-zag-1"})
        self.assertNotIn("ch-dk-2", zones("w-09-eu-large-model"))
        self.assertNotIn("fr-par-1", zones("w-01-dach-research"))

    def test_illegal_placement_raises(self):
        m = model()
        with self.assertRaises(Illegal):
            m.assert_legal(m.workloads["w-02-ch-reporting"], "de-fra-1", "20000000-0000-4000-8000-000000000003")
        with self.assertRaises(Illegal):
            m.assert_legal(m.workloads["w-08-ch-imaging"], "de-fra-1", "20000000-0000-4000-8000-000000000008")


class PolicyTests(unittest.TestCase):
    def test_quota_fit(self):
        m = model()
        u = Usage()
        mega = m.profiles["20000000-0000-4000-8000-000000000003"]
        for _ in range(10):
            self.assertTrue(u.fits(m, mega))
            u.add(mega)
        self.assertFalse(u.fits(m, mega))                 # 320 vCPU reached

    def test_decide_run_prefers_clean_hours(self):
        clean = [50.0] * 6 + [300.0] * 18
        self.assertEqual(decide_run(10, 90, 1, clean)[0], True)
        self.assertEqual(decide_run(10, 90, 1, [300.0] + clean)[0], False)
        self.assertEqual(decide_run(80, 90, 1, [300.0] + clean), (True, True))   # no slack -> run

    def test_quantile(self):
        self.assertAlmostEqual(quantile([1, 2, 3, 4], 0.5), 2.5)


class EndToEnd(unittest.TestCase):
    def test_all_workloads_meet_deadlines_and_vms_are_deleted(self):
        for seed in (1, 2, 3):
            env, ctl = run_sim(seed)
            self.assertEqual(env.late(), {}, f"seed {seed}")
            self.assertEqual(env.vms, {}, f"seed {seed}: idle VMs left")

    def test_saves_carbon_vs_always_on(self):
        for seed in (1, 2, 3):
            smart, _ = run_sim(seed, True)
            base, _ = run_sim(seed, False)
            self.assertEqual(base.late(), {})
            self.assertLess(smart.carbon, base.carbon, f"seed {seed}")


class PauseValueAndParking(unittest.TestCase):
    def test_excess_and_dirty_stretch(self):
        clean = [50.0] * 6 + [300.0] * 18
        self.assertEqual(excess(10, 90, 1, clean), 0.0)              # clean hour: nothing to gain
        self.assertGreater(excess(10, 90, 1, [300.0] + clean), 1.0)  # far above the run threshold
        self.assertEqual(dirty_run_h(10, 90, 1, [300.0] * 5 + clean), 5)

    def test_only_cheap_to_restart_jobs_are_parked_and_restarts_stay_legal(self):
        for seed in (1, 2):
            env, ctl = run_sim(seed)
            parked = {}
            for t, ev, name, zone in env.log:
                if ev == "create" and name != "orch-starter":
                    wid = name[5:].rsplit("-", 2)[0]
                    parked[wid] = parked.get(wid, 0) + 1
            for wid, n in parked.items():
                w = ctl.m.workloads[wid]
                if n > int(w["max_workers"]):        # recreated -> must be a portable job
                    self.assertFalse(w["restart_from_zero"], wid)
                    self.assertLessEqual(w["checkpoint_seconds"], 1800, wid)
                    self.assertLessEqual(w["input_seconds"], 900, wid)
            self.assertEqual(env.late(), {}, f"seed {seed}")


class EndedRun(unittest.TestCase):
    def test_finished_run_is_reported_not_claimed_as_success(self):
        class Done:
            def status(self):
                return {"state": "COMPLETED", "qualification": "QUALIFIED", "virtual_time": "2025-10-05T00:00:00Z",
                        "workloads": [{"final_output_delivered": i < 2} for i in range(15)]}
        msgs = []
        ctl = Controller(Done(), model(), log=msgs.append)
        self.assertEqual(ctl.tick(), "ended")
        self.assertIn("2 of 15", msgs[0])


class QuotaFallback(unittest.TestCase):
    def test_waits_instead_of_taking_a_plan_that_cannot_meet_the_deadline(self):
        from orchestrator.core import parse_ts, pick, rank_options
        m = model()
        w = m.workloads["w-12-late-delivery"]
        opts = rank_options(m, w, parse_ts("2025-10-02T09:36:00Z"), 0.0, {})
        u = Usage()
        u.vms, u.vcpus = 12, 304                      # only 16 vCPU left: just a "huge" would fit
        self.assertIsNone(pick(opts, m, u))           # huge needs 61 h of the 62 h left -> wait for a mega
        u.vcpus = 280                                 # 40 vCPU free: a mega fits
        o = pick(opts, m, u)
        self.assertEqual(m.profiles[o.pid]["size"], "mega")


class GridCredentials(unittest.TestCase):
    ACCOUNT = {"endpoint": "https://host/runs/abc123/cloud/v2"}
    GRID = "https://host/runs/abc123/grid/v4"

    def resolve(self, env, files=()):
        with tempfile.TemporaryDirectory() as d:
            for name, blob in files:
                (Path(d) / name).write_text(json.dumps(blob))
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                return grid_credentials(dict(self.ACCOUNT), str(Path(d) / "greenops-exo.json"))

    def test_environment_wins_and_is_what_judging_passes(self):
        self.assertEqual(self.resolve({"GREENOPS_GRID_ENDPOINT": "https://host/runs/abc123/grid/v4",
                                       "GREENOPS_GRID_SECRET": "from-env"}),
                         (self.GRID, "from-env"))

    def test_secret_from_json_file_and_endpoint_derived(self):
        self.assertEqual(self.resolve({}, [("greenops-grid.json", {"secret": "from-file"})]),
                         (self.GRID, "from-file"))

    def test_secret_field_on_the_cloud_config(self):
        # Always resolve inside a temp dir: a relative path would pick up a real
        # greenops-grid.json from the working directory and leak its secret into the diff.
        with tempfile.TemporaryDirectory() as d:
            acct = dict(self.ACCOUNT, grid_secret="inline")
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(grid_credentials(acct, str(Path(d) / "greenops-exo.json")),
                                 (self.GRID, "inline"))

    def test_no_secret_anywhere_yields_nothing_to_run_with(self):
        self.assertEqual(self.resolve({}), (None, None))


class StatusHelperCompatibility(unittest.TestCase):
    """The submission image keeps the STARTER image's examples/python, not ours.

    So work.status may be the older one that takes no timeout. Calling it with the keyword
    raises TypeError on every tick until the error budget aborts the run, which is exactly
    what happened live. CloudEnv.status must work against either signature.
    """

    def call_status(self, helper):
        import orchestrator.cloud as cloud
        env = cloud.CloudEnv.__new__(cloud.CloudEnv)      # no network, no discovery
        env.account = {"endpoint": "https://host/runs/x/cloud/v2"}
        env.status_timeout = 4.0
        takes = "timeout" in inspect.signature(helper).parameters
        with unittest.mock.patch.object(cloud.work, "status", helper), \
             unittest.mock.patch.object(cloud, "_STATUS_TAKES_TIMEOUT", takes):
            return cloud.CloudEnv.status(env)

    def test_modern_helper_receives_the_shortened_timeout(self):
        seen = {}

        def helper(account, timeout=10):
            seen["timeout"] = timeout
            return {"state": "RUNNING"}

        self.assertEqual(self.call_status(helper), {"state": "RUNNING"})
        self.assertEqual(seen["timeout"], 4.0)

    def test_starter_image_helper_without_timeout_still_works(self):
        def helper(account):                               # the base image's signature
            return {"state": "RUNNING"}

        self.assertEqual(self.call_status(helper), {"state": "RUNNING"})


class LoopPacing(unittest.TestCase):
    """At 720x a wall second is 12 virtual minutes, so idle wall time is lost control."""

    def drive(self, tick_cost, outcome="wait", poll=1.0):
        slept, clock = [], [0.0]

        class Ctl:
            def __init__(self):
                self.n = 0

            def adopt(self):
                pass

            def tick(self):
                clock[0] += tick_cost
                self.n += 1
                if self.n >= 3:
                    return "ended"
                if outcome == "raise":
                    raise RuntimeError("read timed out")
                return outcome

        with unittest.mock.patch.object(controller.time, "monotonic", lambda: clock[0]), \
             unittest.mock.patch.object(controller.time, "sleep", slept.append):
            controller.run_loop(Ctl(), poll=poll, log=lambda *_: None)
        return slept

    def test_a_slow_tick_does_not_also_pay_the_poll_interval(self):
        self.assertEqual(self.drive(4.0), [0.0, 0.0])       # tick already outspent the interval

    def test_a_fast_tick_still_keeps_the_cadence(self):
        self.assertEqual(self.drive(0.25), [0.75, 0.75])

    def test_an_instantly_failing_tick_cannot_spin_hot(self):
        self.assertEqual(self.drive(0.0, outcome="raise"), [1.0, 1.0])
