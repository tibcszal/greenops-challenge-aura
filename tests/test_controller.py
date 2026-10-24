"""Controller guardrails: legality, full-pack feasibility, quota and run scoping."""
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller import timeline
from controller.catalog import Catalog
from controller.endpoints import Account, check_cloud_endpoint
from controller.legality import assert_legal, legal_placements, zone_allowed
from controller.planner import candidates, plan
from controller.quota import QuotaLedger
from controller.signing import authorization
from controller.sizing import compute_seconds, effective_throughput, estimate
from controller.state import State, VMRecord
from controller.workapi import Status

CATALOG = Catalog.load()
PLAN = plan(CATALOG)


class Legality(unittest.TestCase):
    def test_forbidden_countries_are_refused(self):
        paris = CATALOG.zone("fr-par-1")
        for workload in CATALOG.workloads:
            forbids_france = "FR" in workload.forbidden_countries
            self.assertEqual(zone_allowed(workload, paris), not forbids_france
                             and (not workload.allowed_countries
                                  or "FR" in workload.allowed_countries),
                             workload.id)

    def test_eu_residency_excludes_switzerland(self):
        eu_workloads = [w for w in CATALOG.workloads if w.residency == "EU"]
        self.assertTrue(eu_workloads)
        for workload in eu_workloads:
            for slug in ("ch-dk-2", "ch-gva-2"):
                self.assertFalse(zone_allowed(workload, CATALOG.zone(slug)), workload.id)

    def test_swiss_workloads_stay_in_switzerland(self):
        for workload in CATALOG.workloads:
            if workload.residency != "CH":
                continue
            zones = {z.slug for _, z in legal_placements(workload, CATALOG)}
            self.assertTrue(zones)
            self.assertEqual({CATALOG.zone(s).country for s in zones}, {"CH"})

    def test_every_workload_has_a_legal_placement(self):
        for workload in CATALOG.workloads:
            self.assertTrue(legal_placements(workload, CATALOG), workload.id)

    def test_assert_legal_rejects_wrong_country(self):
        swiss = CATALOG.workload("w-02-ch-reporting")
        profile = CATALOG.profile("20000000-0000-4000-8000-000000000002")
        with self.assertRaises(ValueError):
            assert_legal(swiss, profile, CATALOG.zone("de-fra-1"))

    def test_assert_legal_rejects_insufficient_gpu_memory(self):
        large_model = CATALOG.workload("w-09-eu-large-model")
        a30 = next(p for p in CATALOG.profiles if p.family == "gpua30")
        with self.assertRaises(ValueError):
            assert_legal(large_model, a30, CATALOG.zone("ch-gva-2"))

    def test_candidate_profiles_satisfy_family_and_memory_rules(self):
        for workload in CATALOG.workloads:
            for option in candidates(workload, CATALOG):
                self.assertIn(option.profile.family, workload.allowed_families)
                self.assertEqual(option.profile.work_unit, workload.work_unit)
                if workload.minimum_gpu_memory_gib:
                    self.assertGreaterEqual(option.profile.gpu_memory_gib,
                                            workload.minimum_gpu_memory_gib)
                self.assertLessEqual(option.workers, workload.max_workers)


class FullPackPlan(unittest.TestCase):
    def test_plans_every_workload(self):
        self.assertEqual(len(PLAN.assignments), len(CATALOG.workloads))
        self.assertEqual({a.workload_id for a in PLAN.assignments},
                         {w.id for w in CATALOG.workloads})
        self.assertEqual(PLAN.scenario_id, CATALOG.scenario_id)

    def test_every_assignment_is_legal(self):
        for assignment in PLAN.assignments:
            assert_legal(CATALOG.workload(assignment.workload_id),
                         CATALOG.profile(assignment.profile_id),
                         CATALOG.zone(assignment.zone_slug))

    def test_every_workload_meets_its_deadline(self):
        for assignment in PLAN.assignments:
            self.assertGreater(assignment.estimate.slack_seconds, 0, assignment.workload_id)
            self.assertLess(assignment.estimate.utilization, Decimal(1), assignment.workload_id)

    def test_plan_respects_run_wide_quota_over_time(self):
        self.assertIsNone(timeline.check(PLAN, CATALOG))
        self.assertLessEqual(PLAN.peak.vcpus, CATALOG.max_vcpus)
        for family, used in timeline.peak_cards(
                timeline.occupancies(PLAN, CATALOG), 120 * 3600).items():
            self.assertLessEqual(used, CATALOG.gpu_family_quotas[family], family)

    def test_reserved_quota_is_honoured(self):
        reserved = plan(CATALOG, reserve_vms=2, reserve_vcpus=20)
        self.assertLessEqual(reserved.peak.vcpus, CATALOG.max_vcpus - 20)
        self.assertIsNone(timeline.check(reserved, CATALOG, reserve_vms=2, reserve_vcpus=20))

    def test_vm_names_are_unique_and_deterministic(self):
        names = [name for a in PLAN.assignments for name in a.vm_names()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(PLAN.assignment("w-01-dach-research").vm_names()[0],
                         "w-01-dach-research-0")


class Sizing(unittest.TestCase):
    def test_single_worker_has_no_sync_penalty(self):
        profile = CATALOG.profile("20000000-0000-4000-8000-000000000003")
        self.assertEqual(effective_throughput(profile, 1, Decimal("0.08")), profile.throughput)

    def test_extra_workers_are_discounted(self):
        profile = CATALOG.profile("20000000-0000-4000-8000-000000000003")
        solo = effective_throughput(profile, 1, Decimal("0.08"))
        pair = effective_throughput(profile, 2, Decimal("0.08"))
        self.assertLess(pair, solo * 2)
        self.assertGreater(pair, solo)

    def test_cpu_work_converts_vcpu_hours_to_seconds(self):
        workload = CATALOG.workload("w-01-dach-research")
        profile = CATALOG.profile("20000000-0000-4000-8000-000000000003")
        self.assertEqual(compute_seconds(workload, profile, 1),
                         workload.required_work / profile.throughput * 3600)

    def test_total_time_includes_boot_staging_and_output(self):
        workload = CATALOG.workload("w-06-large-etl")
        profile = CATALOG.profile("20000000-0000-4000-8000-000000000003")
        option = estimate(workload, profile, CATALOG.zone("de-fra-1"), 1)
        overhead = option.total_seconds - option.compute_seconds
        self.assertEqual(overhead, profile.boot_seconds + workload.input_seconds
                         + workload.output_seconds)


class Quota(unittest.TestCase):
    def test_gpu_vms_consume_vcpu_and_card_quota(self):
        ledger = QuotaLedger.for_catalog(CATALOG)
        rtx = next(p for p in CATALOG.profiles
                   if p.family == "gpurtx6000pro" and p.size == "large")
        ledger.add(rtx)
        self.assertEqual(ledger.vcpus, rtx.cpu_cores)
        self.assertEqual(ledger.cards["gpurtx6000pro"], 4)
        self.assertIsNotNone(ledger.violation(rtx))

    def test_vcpu_ceiling_is_enforced(self):
        ledger = QuotaLedger.for_catalog(CATALOG)
        titan = CATALOG.profile("20000000-0000-4000-8000-000000000004")
        self.assertTrue(ledger.fits(titan, 8))
        self.assertFalse(ledger.fits(titan, 9))

    def test_removal_frees_quota(self):
        ledger = QuotaLedger.for_catalog(CATALOG)
        huge = CATALOG.profile("20000000-0000-4000-8000-000000000002")
        ledger.add(huge, 2)
        ledger.remove(huge, 2)
        self.assertEqual((ledger.vms, ledger.vcpus), (0, 0))


class RunScoping(unittest.TestCase):
    BASE = "http://localhost:8080/runs/fictional-run/cloud/v2"

    def test_zone_endpoint_must_stay_in_the_run(self):
        check_cloud_endpoint("http://localhost:8080/runs/fictional-run/cloud/ch-gva-2/v2",
                             self.BASE)
        for url in ["https://api-at-vie-1.exoscale.com/v2",
                    "http://localhost:8080/runs/other/cloud/v2",
                    "http://elsewhere.invalid/runs/fictional-run/cloud/v2",
                    self.BASE + "?endpoint=other"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                check_cloud_endpoint(url, self.BASE)

    def test_derived_endpoints_share_the_run_prefix(self):
        account = Account("key", "secret", self.BASE)
        self.assertEqual(account.work_endpoint,
                         "http://localhost:8080/runs/fictional-run/work/v1")
        self.assertEqual(account.resolved_grid_endpoint(),
                         "http://localhost:8080/runs/fictional-run/grid/v4")

    def test_grid_endpoint_from_another_run_is_refused(self):
        account = Account("key", "secret", self.BASE,
                          grid_endpoint="http://localhost:8080/runs/other/grid/v4")
        with self.assertRaises(ValueError):
            account.resolved_grid_endpoint()

    def test_signature_covers_the_full_prefixed_path(self):
        header = authorization("key", "secret", "GET",
                               "http://localhost:8080/runs/fictional-run/work/v1/status")
        self.assertTrue(header.startswith("EXO2-HMAC-SHA256 credential=key,"))
        other = authorization("key", "secret", "GET", "http://localhost:8080/work/v1/status")
        self.assertNotEqual(header.split("signature=")[1], other.split("signature=")[1])

    def test_receipt_refuses_another_runs_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            state = State(path, self.BASE)
            state.add(VMRecord("vm-1", "w-01-dach-research-0", "de-fra-1",
                               "w-01-dach-research", "profile-1"))
            self.assertEqual(len(State(path, self.BASE).instances), 1)
            with self.assertRaises(ValueError):
                State(path, "http://localhost:8080/runs/other/cloud/v2")

    def test_status_parsing_exposes_delivery_not_progress(self):
        status = Status.parse(json.loads(
            '{"run_id":"r","virtual_time":"2025-09-30T04:00:00Z","speed_multiplier":720,'
            '"state":"RUNNING","qualification":"QUALIFIED","workloads":[{"id":"w-01",'
            '"state":"running","required_work":"1000","completed_work":"1000.000000",'
            '"lost_work":"0","final_output_delivered":false,"assignments":[]}]}'))
        self.assertEqual(status.workload("w-01").progress, 1.0)
        self.assertEqual(status.delivered, set())
        self.assertFalse(status.terminal)


if __name__ == "__main__":
    unittest.main()
