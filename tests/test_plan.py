import json
import tempfile
import unittest
from pathlib import Path

from busagent.errors import Rejected
from busagent.plan import PlanItem, PlanProposal, derive_contract
from busagent.runtime import Runtime

GOAL = "张三的订单 1001 需要补发 MUG-WHITE"


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.tmp.name) / "p")

    def tearDown(self) -> None:
        self.rt.close()
        self.tmp.cleanup()

    def test_proposal_waits_for_plan_approval(self) -> None:
        wf = self.rt.propose_plan(GOAL)
        row = self.rt.store.workflow(wf)
        self.assertEqual(row["state"], "WAIT_PLAN_APPROVAL")
        proposal = json.loads(row["proposal"])
        self.assertEqual(len(proposal["items"]), 6)
        self.assertEqual(row["contract"], None)
        self.assertEqual(self.rt.store.shipment_count(), 0)

    def test_clerk_cannot_approve_a_plan(self) -> None:
        wf = self.rt.propose_plan(GOAL)
        with self.assertRaises(Rejected):
            self.rt.approve_plan(wf, "clerk")

    def test_approving_plan_derives_contract_and_runs(self) -> None:
        wf = self.rt.propose_plan(GOAL)
        state = self.rt.approve_plan(wf, "supervisor")
        self.assertEqual(state, "WAIT_APPROVAL")
        contract = json.loads(self.rt.store.workflow(wf)["contract"])
        self.assertEqual(len(contract["requires"]), 6)
        self.assertEqual(self.rt.store.shipment_count(), 0)

        self.rt.approve(wf, "supervisor")
        self.assertEqual(self.rt.resume(wf), "DONE")
        self.assertEqual(self.rt.store.shipment_count(), 1)
        self.assertIn("SHP-", self.rt.answer(wf))

    def test_rejecting_the_plan_stops_the_workflow(self) -> None:
        wf = self.rt.propose_plan(GOAL)
        self.assertEqual(self.rt.reject_plan(wf, "不做"), "DENIED")
        self.assertEqual(self.rt.store.shipment_count(), 0)

    def test_plan_without_acceptance_is_refused(self) -> None:
        class BadPlanner:
            def propose(self, goal: str) -> PlanProposal:
                return PlanProposal(goal, (PlanItem("s1", "只是描述"),))

        with self.assertRaises(Rejected):
            self.rt.propose_plan(GOAL, planner=BadPlanner())

    def test_contract_needs_a_supported_assertion_kind(self) -> None:
        proposal = PlanProposal(GOAL, (PlanItem("s1", "x", {"kind": "vibes"}),))
        with self.assertRaises(Rejected):
            derive_contract(proposal)

    def test_plan_state_does_not_run_the_loop(self) -> None:
        wf = self.rt.propose_plan(GOAL)
        self.assertEqual(self.rt.resume(wf), "WAIT_PLAN_APPROVAL")
        self.assertEqual(self.rt.store.shipment_count(), 0)


if __name__ == "__main__":
    unittest.main()
