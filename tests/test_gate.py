import tempfile
import unittest
from pathlib import Path

from busagent.contracts import ToolCall
from busagent.gate import Gate, digest, idem_key
from busagent.store import Store
from busagent.tools import build_registry


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.registry = build_registry(self.store)
        self.gate = Gate(self.store, self.registry, "clerk")
        self.store.create_workflow("wf_t", "goal", "clerk")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_digest_binds_arguments(self) -> None:
        spec = self.registry.spec("submit_request")
        base = {"order_id": 1001, "sku": "MUG-WHITE", "qty": 2, "note": "x"}
        changed = dict(base, note="y")
        self.assertNotEqual(digest(spec, base), digest(spec, changed))

    def test_idem_is_scoped_to_workflow(self) -> None:
        spec = self.registry.spec("submit_request")
        args = {"order_id": 1001, "sku": "MUG-WHITE", "qty": 2, "note": "x"}
        self.assertEqual(idem_key("wf_t", spec, args), idem_key("wf_t", spec, args))
        self.assertNotEqual(idem_key("wf_t", spec, args), idem_key("wf_u", spec, args))

    def test_write_waits_for_approval(self) -> None:
        call = ToolCall("c1", "submit_request",
                        {"order_id": 1001, "sku": "MUG-WHITE", "qty": 2, "note": "x"})
        prior = [{"draft": {"order_id": 1001, "sku": "MUG-WHITE", "qty": 2, "note": "x"}}]
        outcome = self.gate.dispatch("wf_t", call, prior)
        self.assertEqual(outcome.status, "wait_approval")
        self.assertTrue(outcome.digest)
        self.assertEqual(self.store.shipment_count(), 0)

    def test_grounding_rejects_fabricated_customer(self) -> None:
        call = ToolCall("c1", "get_order", {"customer_id": 2, "order_id": 1001})
        prior = [{"candidates": [{"id": 1, "name": "张三"}]}]
        outcome = self.gate.dispatch("wf_t", call, prior)
        self.assertEqual(outcome.status, "rejected")
        self.assertIn("来源不可信", outcome.error)

    def test_intern_has_no_read_permission(self) -> None:
        gate = Gate(self.store, self.registry, "intern")
        outcome = gate.dispatch("wf_t", ToolCall("c", "find_customer", {"name": "张三"}), [])
        self.assertEqual(outcome.status, "denied")


if __name__ == "__main__":
    unittest.main()
