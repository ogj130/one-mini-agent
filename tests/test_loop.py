import tempfile
import unittest
from pathlib import Path

from busagent.cli import QUESTION
from busagent.config import Limits
from busagent.models import (ScriptedModel, call, feedback_script,
                             retry_after_reject_script)
from busagent.runtime import Runtime


class LoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normal_flow_needs_approval_then_commits_once(self) -> None:
        rt = Runtime(self.base / "a")
        wf = rt.start(QUESTION)
        self.assertEqual(rt.status(wf)["state"], "WAIT_APPROVAL")
        self.assertEqual(rt.store.shipment_count(), 0)

        rt.approve(wf, "supervisor")
        self.assertEqual(rt.resume(wf), "DONE")
        self.assertEqual(rt.store.shipment_count(), 1)
        rt.close()

    def test_resume_in_a_new_runtime_keeps_context(self) -> None:
        rt = Runtime(self.base / "b")
        wf = rt.start(QUESTION)
        rt.close()

        reopened = Runtime(self.base / "b")       # 模拟新进程
        reopened.approve(wf, "supervisor")
        self.assertEqual(reopened.resume(wf), "DONE")
        self.assertEqual(reopened.store.shipment_count(), 1)
        reopened.close()

    def test_revise_breaks_old_approval(self) -> None:
        rt = Runtime(self.base / "c")
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        before = rt.status(wf)["pending"]["digest"]
        rt.revise(wf, "改成加急")
        after = rt.status(wf)["pending"]["digest"]
        self.assertNotEqual(before, after)
        self.assertEqual(rt.resume(wf), "WAIT_APPROVAL")
        self.assertEqual(rt.store.shipment_count(), 0)
        rt.close()

    def test_rejection_is_fed_back_and_retried(self) -> None:
        rt = Runtime(self.base / "d", model=ScriptedModel(retry_after_reject_script(QUESTION)))
        wf = rt.start(QUESTION)
        kinds = [e["kind"] for e in rt.store.events(wf)]
        self.assertIn("rejected", kinds)
        self.assertEqual(rt.status(wf)["state"], "WAIT_APPROVAL")
        rt.approve(wf, "supervisor")
        self.assertEqual(rt.resume(wf), "DONE")
        self.assertEqual(rt.store.shipment_count(), 1)
        rt.close()

    def test_unknown_reconciles_without_resubmitting(self) -> None:
        rt = Runtime(self.base / "e")
        rt.store.fail_next_commit = True
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        self.assertEqual(rt.resume(wf), "UNKNOWN")
        for _ in range(3):
            self.assertEqual(rt.resume(wf), "UNKNOWN")
        self.assertEqual(rt.store.shipment_count(), 1)

        rt.store.exec("UPDATE shipments SET visible = 1 WHERE visible = 0")
        self.assertEqual(rt.resume(wf), "DONE")
        self.assertEqual(rt.store.shipment_count(), 1)
        rt.close()

    def test_reject_with_reason_reproposes_and_commits_once(self) -> None:
        rt = Runtime(self.base / "g", model=ScriptedModel(feedback_script(QUESTION)))
        wf = rt.start(QUESTION)
        first = rt.status(wf)["pending"]["digest"]
        self.assertEqual(rt.reject(wf, "补错东西了，应该补杯盖"), "WAIT_APPROVAL")

        second = rt.status(wf)["pending"]["digest"]
        self.assertNotEqual(first, second)
        self.assertEqual(rt.store.shipment_count(), 0)

        rt.approve(wf, "supervisor")
        self.assertEqual(rt.resume(wf), "DONE")
        skus = [row["sku"] for row in rt.store.q("SELECT sku FROM shipments")]
        self.assertEqual(skus, ["CUP-LID"])
        rt.close()

    def test_reject_after_approval_revokes_it(self) -> None:
        rt = Runtime(self.base / "i")
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        self.assertEqual(rt.reject(wf, "改成杯盖"), "WAIT_APPROVAL")
        self.assertEqual(rt.resume(wf), "WAIT_APPROVAL")   # 旧批准已作废
        self.assertEqual(rt.store.shipment_count(), 0)
        self.assertTrue(all(a["active"] == 0 for a in
                            (dict(r) for r in rt.store.approvals(wf))))
        rt.close()

    def test_reject_requires_a_pending_action(self) -> None:
        rt = Runtime(self.base / "h")
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        rt.resume(wf)
        with self.assertRaises(Exception):
            rt.reject(wf, "too late")
        rt.close()

    def test_budget_stops_a_looping_model(self) -> None:
        repeat = lambda messages: call("find_customer", name="张三")  # noqa: E731
        rt = Runtime(self.base / "f", limits=Limits(max_turns=3, max_tool_calls=8),
                     model=ScriptedModel([repeat] * 10))
        wf = rt.start(QUESTION)
        self.assertEqual(rt.status(wf)["state"], "FAILED")
        rt.close()


if __name__ == "__main__":
    unittest.main()
