import tempfile
import unittest
from pathlib import Path

from busagent.cli import QUESTION
from busagent.runtime import Runtime
from busagent.session import SessionAction, ScriptedSessionModel, SessionLoop


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.tmp.name) / "s")

    def tearDown(self) -> None:
        self.rt.close()
        self.tmp.cleanup()

    def _loop(self, actions) -> SessionLoop:
        return SessionLoop(self.rt.store, self.rt,
                           ScriptedSessionModel(actions), "s_test")

    def test_open_creates_a_workflow_in_the_session(self) -> None:
        loop = self._loop([SessionAction("open", QUESTION)])
        turn = loop.handle(QUESTION)

        self.assertEqual(turn.action.kind, "open")
        workflows = self.rt.store.workflows_in_session("s_test")
        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0]["state"], "WAIT_APPROVAL")
        self.assertEqual(len(self.rt.store.session_messages("s_test")), 2)

    def test_advance_with_correction_rebinds_approval(self) -> None:
        loop = self._loop([
            SessionAction("open", QUESTION),
            SessionAction("advance", "补错东西了，应该补杯盖"),
        ])
        loop.handle(QUESTION)
        wf = self.rt.store.workflows_in_session("s_test")[0]["id"]
        before = self.rt.status(wf)["pending"]["digest"]

        turn = loop.handle("补错东西了，应该补杯盖")
        after = self.rt.status(wf)["pending"]["digest"]

        self.assertEqual(turn.wf, wf)
        self.assertNotEqual(before, after)
        self.assertEqual(self.rt.status(wf)["state"], "WAIT_APPROVAL")
        self.assertEqual(self.rt.store.shipment_count(), 0)
        self.assertTrue(all(row["active"] == 0 for row in self.rt.store.approvals(wf)))

    def test_status_answers_without_creating_a_workflow(self) -> None:
        loop = self._loop([
            SessionAction("open", QUESTION),
            SessionAction("status"),
        ])
        loop.handle(QUESTION)
        turn = loop.handle("/status")

        self.assertEqual(turn.action.kind, "status")
        self.assertIn("WAIT_APPROVAL", turn.reply)
        self.assertEqual(len(self.rt.store.workflows_in_session("s_test")), 1)

    def test_new_topic_keeps_the_old_workflow_alive(self) -> None:
        loop = self._loop([
            SessionAction("open", QUESTION),
            SessionAction("open", "李四的订单 1002 缺什么，帮我补发"),
        ])
        loop.handle(QUESTION)
        loop.handle("李四的订单 1002 缺什么，帮我补发")

        workflows = self.rt.store.workflows_in_session("s_test")
        self.assertEqual(len(workflows), 2)
        self.assertTrue(all(row["state"] == "WAIT_APPROVAL" for row in workflows))

    def test_ask_stops_without_touching_workflows(self) -> None:
        loop = self._loop([SessionAction("ask", "你是指订单 1001 还是 1002？")])
        turn = loop.handle("那个订单")

        self.assertEqual(turn.action.kind, "ask")
        self.assertEqual(turn.reply, "你是指订单 1001 还是 1002？")
        self.assertEqual(self.rt.store.workflows_in_session("s_test"), [])


if __name__ == "__main__":
    unittest.main()
