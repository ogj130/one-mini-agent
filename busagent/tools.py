"""Business tools. Each one takes `(args, workflow_id)`; closures capture the store.

The `update_plan` tool exists on purpose: a plan is an *artifact* the loop
maintains, not a stage that owns execution. This is the same shape Codex uses
(`update_plan`) and Claude Code uses (todo state).
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import ToolRegistry, ToolSpec
from .errors import CommitTimeout, NotFound
from .store import Store


def _labels(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise ValueError("plan items must be a list")
    return [{"step": str(i["step"]), "status": str(i["status"])} for i in items]


def build_registry(store: Store) -> ToolRegistry:
    """Wire tool specs to implementations. Read-only tools never write."""

    def find_customer(args: dict[str, Any], wf: str) -> Any:
        return {"candidates": store.find_customers(args["name"])}

    def get_order(args: dict[str, Any], wf: str) -> Any:
        return store.find_order(args["order_id"], args["customer_id"])

    def get_missing(args: dict[str, Any], wf: str) -> Any:
        return {"missing": store.missing_items(args["order_id"])}

    def draft_request(args: dict[str, Any], wf: str) -> Any:
        if not store.missing_items(args["order_id"]):
            raise NotFound(f"订单 {args['order_id']} 没有缺件记录")
        return {"draft": {"order_id": args["order_id"], "sku": args["sku"],
                          "qty": args["qty"], "note": args.get("note", "")}}

    def submit_request(args: dict[str, Any], wf: str) -> Any:
        # _idem_key 由 gate 注入；_fail_after_commit 只在 demo 里模拟回包丢失。
        idem = args["_idem_key"]
        visible = not bool(args.get("_fail_after_commit"))
        out = store.insert_shipment(idem, args["order_id"], args["sku"],
                                    args["qty"], args.get("note", ""), visible)
        if not visible:
            raise CommitTimeout("业务库已写入，但对账查询暂不可见")
        return out

    def verify_shipment(args: dict[str, Any], wf: str) -> Any:
        found = store.find_shipment(args["idem_key"])
        return {"found": found is not None, "shipment": found}

    def update_plan(args: dict[str, Any], wf: str) -> Any:
        row = store.workflow(wf)
        items = json.loads(row["plan"] or "[]")
        for item in items:
            if item["step"] == args["step"]:
                item["status"] = args["status"]
                break
        else:
            items.append({"step": args["step"], "status": args["status"]})
        store.update_workflow(wf, plan=json.dumps(items, ensure_ascii=False))
        return {"plan": items}

    specs = [
        ToolSpec("find_customer", "read", {"name": str}, ("name",), (),
                 "按名字找客户，可能返回多个候选"),
        ToolSpec("get_order", "read", {"customer_id": int, "order_id": int},
                 ("customer_id", "order_id"), ("customer_id",),
                 "customer_id 必须来自上游工具结果"),
        ToolSpec("get_missing", "read", {"order_id": int}, ("order_id",), ("order_id",)),
        ToolSpec("draft_request", "pure",
                 {"order_id": int, "sku": str, "qty": int, "note": str},
                 ("order_id", "sku", "qty"), ("order_id", "sku", "qty")),
        ToolSpec("submit_request", "write",
                 {"order_id": int, "sku": str, "qty": int, "note": str},
                 ("order_id", "sku", "qty"), ("order_id", "sku", "qty"),
                 "写操作：必须有人批准，且自带幂等键"),
        ToolSpec("verify_shipment", "read", {"idem_key": str}, ("idem_key",), ()),
        ToolSpec("update_plan", "pure", {"step": str, "status": str},
                 ("step", "status"), (), "更新当前计划项状态（pending/in_progress/done）"),
    ]
    impls = {
        "find_customer": find_customer,
        "get_order": get_order,
        "get_missing": get_missing,
        "draft_request": draft_request,
        "submit_request": submit_request,
        "verify_shipment": verify_shipment,
        "update_plan": update_plan,
    }
    return ToolRegistry(specs, impls)
