"""Goal contracts and the verifier.

A contract is a list of assertions over things the runtime already records:
business rows, executed tool calls, approvals, and the final answer. It never
calls a model, so "done" cannot be talked into existence.

五种断言：effect / trace / approval / answer / forbid。
为什么不让模型自评：验收标准必须独立于被验收的叙述。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .store import Store

_TABLES = {"shipments"}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    missing: list[str]


def verify_goal(store: Store, wf: str, contract: dict[str, Any],
                answer: str) -> VerifyResult:
    # 只读三类证据：workflow inputs、steps、答案文本；不读 messages。
    row = store.workflow(wf)
    inputs = json.loads(row["inputs"] or "{}")
    steps = store.steps(wf)

    missing: list[str] = []
    for spec in contract.get("requires", []):
        if not _holds(spec, store, wf, inputs, steps, answer):
            missing.append(_describe(spec))
    for spec in contract.get("forbids", []):
        if _violated(spec, store, wf, inputs, steps):
            missing.append("违反禁止条件：" + _describe(spec))
    return VerifyResult(not missing, missing)


# ---------------------------------------------------------------------------
# Assertion kinds
# ---------------------------------------------------------------------------

def _holds(spec: dict[str, Any], store: Store, wf: str, inputs: dict[str, Any],
           steps: list[Any], answer: str) -> bool:
    kind = spec.get("kind")
    if kind == "effect":
        # 业务表是最终真相：答案写得再漂亮，行不存在就是没做。
        count = _effect_count(store, wf, inputs, steps, spec)
        if "count" in spec:
            return count == int(spec["count"])
        if "min" in spec:
            return count >= int(spec["min"])
        return count > 0
    if kind == "trace":
        return bool(_matching_steps(steps, inputs, spec))
    if kind == "approval":
        approvals = [a for a in store.approvals(wf) if a["active"]]
        return len(approvals) >= int(spec.get("min", 1))
    if kind == "answer":
        if not all(text in answer for text in spec.get("contains", [])):
            return False
        for field in spec.get("contains_from_trace", []):
            # 答案里的关键字段必须能在 steps 里找到同值，防止模型编单号。
            value = _trace_value(steps, field)
            if value is None or str(value) not in answer:
                return False
        return True
    return False


def _violated(spec: dict[str, Any], store: Store, wf: str,
              inputs: dict[str, Any], steps: list[Any]) -> bool:
    kind = spec.get("kind", "effect")
    if kind == "trace":
        return len(_matching_steps(steps, inputs, spec)) > int(spec.get("count_gt", 1))
    count = _effect_count(store, wf, inputs, steps, spec)
    return count > int(spec.get("count_gt", 1))


def _effect_count(store: Store, wf: str, inputs: dict[str, Any],
                  steps: list[Any], spec: dict[str, Any]) -> int:
    table = spec.get("table", "")
    if table not in _TABLES:
        return 0
    where = {key: _resolve(value, inputs, steps)
             for key, value in (spec.get("where") or {}).items()}
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    args: list[Any] = []
    if where:
        sql += " WHERE " + " AND ".join(f"{key} = ?" for key in where)
        args = list(where.values())
    row = store.q1(sql, tuple(args))
    return int(row["n"]) if row else 0


def _matching_steps(steps: list[Any], inputs: dict[str, Any],
                    spec: dict[str, Any]) -> list[Any]:
    tool = spec.get("tool")
    wanted = {key: _resolve(value, inputs, steps)
              for key, value in (spec.get("args_match") or {}).items()}
    matched = []
    for step in steps:
        if step["tool"] != tool:
            continue
        args = json.loads(step["args"] or "{}")
        if all(args.get(key) == value for key, value in wanted.items()):
            matched.append(step)
    return matched


def _trace_value(steps: list[Any], field: str) -> Any:
    for step in reversed(steps):
        if not step["result"]:
            continue
        try:
            data = json.loads(step["result"])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and field in data:
            return data[field]
    return None


def _resolve(value: Any, inputs: dict[str, Any], steps: list[Any]) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("$input:"):
        return inputs.get(value[7:])
    if value.startswith("$from:"):
        tool, _, path = value[6:].partition(".")
        for step in reversed(steps):
            if step["tool"] != tool or not step["result"]:
                continue
            data = json.loads(step["result"])
            for part in path.split("."):
                if not part:
                    continue
                data = data.get(part) if isinstance(data, dict) else None
            return data
    return value


def _describe(spec: dict[str, Any]) -> str:
    kind = spec.get("kind")
    if kind == "effect":
        return f"effect {spec.get('table')} {spec.get('where')}"
    if kind == "trace":
        return f"trace {spec.get('tool')}"
    if kind == "approval":
        return "approval"
    if kind == "answer":
        return "answer " + ",".join(spec.get("contains", []))
    return str(spec)
