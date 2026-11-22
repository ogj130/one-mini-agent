"""Plan proposals for the generic path.

A plan is not prose: every item carries an acceptance assertion drawn from the
same vocabulary the verifier uses. Approval derives the contract mechanically,
so the model cannot write a weak contract at finish time.

实验 10：删掉任一 item 的 acceptance，derive_contract 会直接拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import Rejected
from .models import parse_question

ASSERTION_KINDS = {"effect", "trace", "approval", "answer"}


@dataclass(frozen=True)
class PlanItem:
    id: str
    title: str
    acceptance: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "acceptance": self.acceptance}


@dataclass(frozen=True)
class PlanProposal:
    goal: str
    items: tuple[PlanItem, ...]
    forbids: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal,
                "items": [item.to_dict() for item in self.items],
                "forbids": list(self.forbids)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PlanProposal":
        return PlanProposal(
            goal=data.get("goal", ""),
            items=tuple(PlanItem(item["id"], item["title"], item.get("acceptance"))
                        for item in data.get("items", [])),
            forbids=tuple(data.get("forbids", ())),
        )


def derive_contract(proposal: PlanProposal) -> dict[str, Any]:
    """Every plan item must be verifiable; the contract is their union.

    这里不“理解”计划，只做机械转换：没有断言的计划项直接拒绝。
    """
    if not proposal.items:
        raise Rejected("计划为空")
    requires = []
    for item in proposal.items:
        if not item.acceptance:
            raise Rejected(f"计划项 {item.id} 没有验收断言：{item.title}")
        if item.acceptance.get("kind") not in ASSERTION_KINDS:
            raise Rejected(f"计划项 {item.id} 的断言类型不支持")
        requires.append(item.acceptance)
    return {"requires": requires, "forbids": list(proposal.forbids)}


class Planner(Protocol):
    def propose(self, goal: str) -> PlanProposal: ...


class ScriptedPlanner:
    """Deterministic stand-in for a real planner model.

    换真模型时只实现 Planner.propose(goal)；合同派生逻辑一行不用改。
    """

    def propose(self, goal: str) -> PlanProposal:
        name, order = parse_question(goal)
        sku_match = re.search(r"\b([A-Z]{2,}-[A-Z0-9]+)\b", goal)
        sku = sku_match.group(1) if sku_match else "MUG-WHITE"
        if not order:
            raise Rejected("无法规划：缺少订单号")

        items = (
            PlanItem("s1", f"确认客户 {name or '（按订单反查）'}",
                     {"kind": "trace", "tool": "find_customer"}),
            PlanItem("s2", f"读取订单 {order}",
                     {"kind": "trace", "tool": "get_order"}),
            PlanItem("s3", "读取缺件明细",
                     {"kind": "trace", "tool": "get_missing"}),
            PlanItem("s4", "组装并提交补发",
                     {"kind": "effect", "table": "shipments",
                      "where": {"order_id": order, "sku": sku}, "count": 1}),
            PlanItem("s5", "取得批准记录",
                     {"kind": "approval"}),
            PlanItem("s6", "答案包含补发单号",
                     {"kind": "answer", "contains": ["补发申请已提交"],
                      "contains_from_trace": ["shipment_id"]}),
        )
        forbids = ({"kind": "effect", "table": "shipments",
                    "where": {"order_id": order}, "count_gt": 1},)
        return PlanProposal(goal=goal, items=items, forbids=forbids)
