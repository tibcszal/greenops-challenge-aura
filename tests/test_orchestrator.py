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
from orchestrator.controller import Controller    # noqa: E402
from orchestrator.core import Illegal, Model, Usage, decide_run, quantile  # noqa: E402
from orchestrator.run import grid_credentials   # noqa: E402

CATALOG = json.loads((ROOT / "challenge/catalog.json").read_text())
WORKLOADS = json.loads((ROOT / "challenge/workloads.json").read_text())


def model():
    return Model(CATALOG, WORKLOADS)


def run_sim(seed=1, carbon_aware=True, margin=1.4, hours=120):
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
