import json
import tempfile
import unittest
from pathlib import Path

from busagent.flows import FlowCoverageError
from busagent.goal import verify_goal
from busagent.runtime import Runtime
from busagent.states import InvalidTransition

REQUEST = "张三的订单 1001 需要补发 MUG-WHITE"
INPUTS = {"customer_name": "张三", "order_id": 1001, "sku": "MUG-WHITE"}


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.tmp.name) / "f")

    def tearDown(self) -> None:
        self.rt.close()
        self.tmp.cleanup()

    def _run_to_done(self, wf: str) -> None:
        self.rt.approve(wf, "supervisor")
        self.assertEqual(self.rt.resume(wf), "DONE")

    def test_missing_inputs_become_questions(self) -> None:
        wf = self.rt.start_flow("shipment.reship", {"customer_name": "张三"},
                                request_text=REQUEST)
        row = self.rt.store.workflow(wf)
        self.assertEqual(row["state"], "NEEDS_INPUT")
        questions = json.loads(row["questions"])
        self.assertEqual({q["key"] for q in questions}, {"order_id", "sku"})
        self.assertEqual(self.rt.store.messages(wf)[0].content, REQUEST)

    def test_answers_resume_the_flow_and_verify_passes(self) -> None:
        wf = self.rt.start_flow("shipment.reship", {"customer_name": "张三"},
                                request_text=REQUEST)
        state = self.rt.answer_questions(wf, {"order_id": "1001", "sku": "MUG-WHITE"})
        self.assertEqual(state, "WAIT_APPROVAL")

        self._run_to_done(wf)
        self.assertEqual(self.rt.store.shipment_count(), 1)
        self.assertIn("SHP-", self.rt.answer(wf))
        self.assertIn("done", [e["kind"] for e in self.rt.store.events(wf)])

    def test_partial_coverage_is_reported_not_silently_run(self) -> None:
        with self.assertRaises(FlowCoverageError) as ctx:
            self.rt.start_flow("shipment.reship", INPUTS,
                               request_text="补发并通知客户")
        self.assertEqual(ctx.exception.missing, ["notify_customer"])

    def test_partial_run_adds_a_coverage_note(self) -> None:
        wf = self.rt.start_flow("shipment.reship", INPUTS,
                                request_text="补发并通知客户", allow_partial=True)
        self._run_to_done(wf)
        self.assertIn("未覆盖 notify_customer", self.rt.answer(wf))

    def test_verify_detects_missing_and_duplicate_effects(self) -> None:
        wf = self.rt.start_flow("shipment.reship", INPUTS, request_text=REQUEST)
        self._run_to_done(wf)
        contract = json.loads(self.rt.store.workflow(wf)["contract"])
        self.assertTrue(verify_goal(self.rt.store, wf, contract,
                                    self.rt.answer(wf)).ok)

        self.rt.store.exec("DELETE FROM shipments")
        missing = verify_goal(self.rt.store, wf, contract, self.rt.answer(wf)).missing
        self.assertTrue(any("effect shipments" in item for item in missing))

        self.rt.store.insert_shipment("extra", 1001, "MUG-WHITE", 2, "dup", True)
        self.rt.store.insert_shipment("extra2", 1001, "CUP-LID", 2, "dup", True)
        missing = verify_goal(self.rt.store, wf, contract, self.rt.answer(wf)).missing
        self.assertTrue(any("禁止" in item for item in missing))

    def test_terminal_state_cannot_restart(self) -> None:
        wf = self.rt.start_flow("shipment.reship", INPUTS, request_text=REQUEST)
        self._run_to_done(wf)
        with self.assertRaises(InvalidTransition):
            self.rt.store.transition(wf, "RUNNING")


if __name__ == "__main__":
    unittest.main()
