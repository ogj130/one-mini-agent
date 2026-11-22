import tempfile
import unittest
from pathlib import Path

from busagent.cli import QUESTION
from busagent.events import EventLog
from busagent.runtime import Runtime


class StreamingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_deltas_join_to_the_final_answer(self) -> None:
        rt = Runtime(self.base / "s")
        log = EventLog()
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        rt.resume(wf, on_event=log)

        self.assertGreaterEqual(log.kinds().count("model.delta"), 3)
        self.assertIn("补发申请已提交", log.text())
        self.assertEqual(log.kinds()[0], "tool.start")
        self.assertEqual(log.events[0].data["name"], "submit_request")
        self.assertIn("turn.start", log.kinds())
        self.assertEqual(rt.status(wf)["state"], "DONE")
        rt.close()

    def test_tool_events_bracket_each_call(self) -> None:
        rt = Runtime(self.base / "t")
        log = EventLog()
        wf = rt.start(QUESTION, on_event=log)
        rt.approve(wf, "supervisor")
        rt.resume(wf, on_event=log)

        kinds = log.kinds()
        for index, kind in enumerate(kinds):
            if kind != "tool.start":
                continue
            later = kinds[index + 1:]
            self.assertIn("tool.result", later)
            result = log.events[index + 1 + later.index("tool.result")]
            self.assertEqual(result.data["name"], log.events[index].data["name"])
        self.assertEqual(kinds[-1], "state")
        self.assertEqual(rt.status(wf)["state"], "DONE")
        rt.close()

    def test_answer_event_carries_the_grounded_text(self) -> None:
        rt = Runtime(self.base / "u")
        log = EventLog()
        wf = rt.start(QUESTION)
        rt.approve(wf, "supervisor")
        rt.resume(wf, on_event=log)

        answers = [e for e in log.events if e.kind == "answer"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].data["text"], rt.answer(wf))
        self.assertEqual(answers[0].data["ungrounded"], [])
        rt.close()


if __name__ == "__main__":
    unittest.main()
