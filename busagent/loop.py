"""The interleaved loop.

One turn = one model decision. Tool results go back into the conversation and
the model decides again, so the plan emerges instead of being precomputed.
The loop owns control flow; the gate owns side effects. Neither can do the
other's job.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .config import Limits
from .contracts import Message, ToolCall, ToolRegistry
from . import states
from .errors import BudgetExceeded
from .events import Event, EventSink, null_sink
from .goal import verify_goal
from .gate import Gate, GateResult
from .store import Store

TERMINAL = states.TERMINAL
MAX_ANSWER_NUMBERS = 40


class AgentLoop:
    def __init__(self, store: Store, registry: ToolRegistry, model: Any,
                 limits: Limits, actor: str = "clerk",
                 on_event: EventSink = null_sink) -> None:
        self.store = store
        self.registry = registry
        self.model = model
        self.limits = limits
        self.actor = actor
        self.on_event = on_event
        self.gate = Gate(store, registry, actor)

    # ---- public --------------------------------------------------------
    def run(self, wf: str) -> str:
        # resume 不是恢复 Python 调用栈：先读状态，只补 pending 或对账，再进循环。
        row = self.store.workflow(wf)
        state = row["state"]
        if state in TERMINAL:
            return state
        if state == states.WAIT_PLAN_APPROVAL:
            # 计划没批下来之前，loop 不跑。计划是工件，不是触发器。
            return state
        if state == "UNKNOWN":
            state = self._reconcile(wf)
            if state == "UNKNOWN":
                return state
        elif state == "WAIT_APPROVAL":
            state = self._resume_pending(wf)
            if state in ("WAIT_APPROVAL", "UNKNOWN", "DENIED", "FAILED"):
                return state
        return self._loop(wf)

    # ---- main loop -----------------------------------------------------
    def _loop(self, wf: str) -> str:
        while True:
            if self._over_budget(wf):
                return "FAILED"

            # 每轮都从库里重建上下文：进程内存里没有隐藏状态。
            messages = self.store.messages(wf)
            self.on_event(Event("turn.start", {"turn": self.store.workflow(wf)["turns"] + 1}))
            decision = self.model.decide_stream(
                messages, self.registry.schemas(),
                lambda text: self.on_event(Event("model.delta", {"text": text})))
            self.store.bump(wf, turns=1)
            self.store.event(wf, "turn", f"calls={len(decision.calls)}")

            if not decision.wants_tools:
                # 模型说“做完了”只是触发验收；DONE 由 verify_goal 决定。
                outcome = self._finish(wf, decision.content)
                if outcome == "RETRY":
                    continue
                return outcome

            self.store.append_message(wf, Message(
                role="assistant", content=decision.content, tool_calls=decision.calls))

            results = self.store.successful_results(wf)
            for tool_call in decision.calls:
                outcome = self._dispatch(wf, tool_call, results)
                if outcome is None:            # suspended (approval / unknown)
                    return self.store.workflow(wf)["state"]
                if outcome == "denied":
                    return "DENIED"
                if outcome == "failed":
                    return "FAILED"
                if outcome == "ok":
                    results = self.store.successful_results(wf)

    # ---- one tool call -------------------------------------------------
    def _dispatch(self, wf: str, tool_call: ToolCall,
                  results: list[Any]) -> str | None:
        row = self.store.workflow(wf)
        if row["tool_calls"] >= self.limits.max_tool_calls:
            self._fail(wf, f"工具调用超过上限 {self.limits.max_tool_calls}")
            return "failed"

        self.store.bump(wf, tool_calls=1)
        self.on_event(Event("tool.start", {"name": tool_call.name, "args": tool_call.args}))
        outcome = self.gate.dispatch(wf, tool_call, results)
        self.on_event(Event("tool.result", {"name": tool_call.name,
                                            "status": outcome.status, "ms": outcome.ms}))

        if outcome.status == "ok":
            self._record_step(wf, tool_call, outcome, "ok")
            self.store.append_message(wf, Message(
                role="tool", tool_call_id=tool_call.id, name=tool_call.name,
                content=json.dumps(outcome.result, ensure_ascii=False)))
            return "ok"

        if outcome.status == "wait_approval":
            # 先存 pending 再改状态：恢复时要靠 pending 找到这次调用。
            self._save_pending(wf, tool_call, outcome)
            self.store.transition(wf, states.WAIT_APPROVAL)
            self.store.event(wf, "wait_approval", f"digest={outcome.digest}")
            self.on_event(Event("state", {"state": "WAIT_APPROVAL"}))
            return None

        if outcome.status == "unknown":
            self._save_pending(wf, tool_call, outcome)
            self.store.transition(wf, states.UNKNOWN)
            self.store.event(wf, "unknown", f"idem={outcome.idem}")
            self.on_event(Event("state", {"state": "UNKNOWN"}))
            return None

        if outcome.status == "denied":
            self.store.transition(wf, states.DENIED)
            self.store.event(wf, "denied", outcome.error)
            self.on_event(Event("state", {"state": "DENIED"}))
            return "denied"

        # rejected: feed the reason back, let the model correct itself
        self.store.event(wf, "rejected", outcome.error)
        self.store.append_message(wf, Message(
            role="tool", tool_call_id=tool_call.id, name=tool_call.name,
            content=json.dumps({"error": outcome.error}, ensure_ascii=False)))
        return "rejected"

    # ---- suspension / recovery ----------------------------------------
    def _save_pending(self, wf: str, tool_call: ToolCall, outcome: GateResult) -> None:
        self.store.update_workflow(wf, pending=json.dumps({
            "call": {"id": tool_call.id, "name": tool_call.name, "args": tool_call.args},
            "digest": outcome.digest, "idem": outcome.idem,
        }, ensure_ascii=False))

    def _pending(self, wf: str) -> tuple[ToolCall, dict[str, Any]]:
        pending = json.loads(self.store.workflow(wf)["pending"] or "{}")
        raw = pending.get("call") or {}
        tool_call = ToolCall(id=raw.get("id", "pending"), name=raw.get("name", ""),
                             args=raw.get("args") or {})
        return tool_call, pending

    def _resume_pending(self, wf: str) -> str:
        # 重新过一遍 gate：不是“上次批过就直接执行”，而是重新校验当前参数。
        tool_call, _ = self._pending(wf)
        results = self.store.successful_results(wf)
        self.on_event(Event("tool.start", {"name": tool_call.name, "args": tool_call.args}))
        outcome = self.gate.dispatch(wf, tool_call, results)
        self.on_event(Event("tool.result", {"name": tool_call.name,
                                            "status": outcome.status, "ms": outcome.ms}))

        if outcome.status == "ok":
            self._record_step(wf, tool_call, outcome, "ok")
            self.store.append_message(wf, Message(
                role="tool", tool_call_id=tool_call.id, name=tool_call.name,
                content=json.dumps(outcome.result, ensure_ascii=False)))
            self.store.transition(wf, states.RUNNING, pending=None)
            return "RUNNING"

        if outcome.status == "wait_approval":
            self._save_pending(wf, tool_call, outcome)   # digest may have changed
            self.store.transition(wf, states.WAIT_APPROVAL)
            return "WAIT_APPROVAL"

        if outcome.status == "unknown":
            self._save_pending(wf, tool_call, outcome)
            self.store.transition(wf, states.UNKNOWN)
            self.store.event(wf, "unknown", f"idem={outcome.idem} (resume)")
            return "UNKNOWN"

        if outcome.status == "denied":
            self.store.transition(wf, states.DENIED)
            return "DENIED"

        self.store.append_message(wf, Message(
            role="tool", tool_call_id=tool_call.id, name=tool_call.name,
            content=json.dumps({"error": outcome.error}, ensure_ascii=False)))
        self.store.transition(wf, states.RUNNING, pending=None)
        return "RUNNING"

    def _reconcile(self, wf: str) -> str:
        # 实验 4/5：UNKNOWN 只读对账；查不到就保持 UNKNOWN，绝不重提。
        tool_call, pending = self._pending(wf)
        idem = pending.get("idem", "")
        self.on_event(Event("tool.start", {"name": "reconcile", "args": {"idem": idem}}))
        found = self.gate.reconcile(idem) if idem else None
        self.on_event(Event("tool.result", {"name": "reconcile",
                                            "status": "found" if found else "not_found", "ms": 0.0}))
        self.store.event(wf, "reconcile", f"idem={idem} found={bool(found)}")
        if not found:
            return "UNKNOWN"

        outcome = GateResult("ok", result=found, digest=pending.get("digest", ""), idem=idem)
        self._record_step(wf, tool_call, outcome, "reconciled")
        self.store.append_message(wf, Message(
            role="tool", tool_call_id=tool_call.id, name=tool_call.name,
            content=json.dumps(found, ensure_ascii=False)))
        self.store.transition(wf, states.RUNNING, pending=None)
        return "RUNNING"

    # ---- records / finish ---------------------------------------------
    def _record_step(self, wf: str, tool_call: ToolCall, outcome: GateResult,
                     status: str) -> None:
        self.store.append_step(wf, self.store.next_step_seq(wf), tool_call.name,
                               tool_call.args, outcome.digest, outcome.idem, status,
                               result=outcome.result, error=outcome.error, ms=outcome.ms)

    def _finish(self, wf: str, content: str | None) -> str:
        row = self.store.workflow(wf)
        answer = content or self._compose(wf)

        # 实验 7：删掉一条发货记录再跑，effect 断言会立刻变红。
        contract = json.loads(row["contract"] or "null")
        if contract:
            result = verify_goal(self.store, wf, contract, answer)
            self.on_event(Event("verify", {"ok": result.ok, "missing": result.missing}))
            if not result.ok:
                attempts = self.store.bump_verify(wf)
                self.store.event(wf, "verify_failed", "; ".join(result.missing))
                # 实验 7 的延伸：连续 3 次补不齐，就交给人，不再让模型空转。
                if attempts >= 3:
                    self.store.transition(wf, states.NEEDS_REVIEW, answer=answer)
                    self.on_event(Event("state", {"state": states.NEEDS_REVIEW}))
                    return states.NEEDS_REVIEW
                self.store.append_message(wf, Message(
                    role="system",
                    content="验收未通过：" + "；".join(result.missing)))
                return "RETRY"

        coverage = json.loads(row["coverage"] or "{}")
        if coverage.get("missing"):
            answer += ("\n注意：本次仅覆盖 " + ", ".join(coverage.get("covered", []))
                       + "；未覆盖 " + ", ".join(coverage["missing"]) + "，需要另走一步。")

        ungrounded = self._ungrounded_numbers(wf, answer)
        self.store.transition(wf, states.DONE, answer=answer)
        self.store.event(wf, "done", f"ungrounded={ungrounded}")
        if ungrounded:
            self.store.event(wf, "ungrounded", ",".join(ungrounded))
        self.on_event(Event("answer", {"text": answer, "ungrounded": ungrounded}))
        self.on_event(Event("state", {"state": states.DONE}))
        return states.DONE

    def _compose(self, wf: str) -> str:
        """Stand-in for the model's final message: numbers taken from records."""
        steps = [s for s in self.store.steps(wf) if s["status"] in ("ok", "reconciled")]
        submitted = next((json.loads(s["result"]) for s in reversed(steps)
                          if s["tool"] == "submit_request" and s["result"]), None)
        if submitted:
            return (f"结论：订单 {submitted['order_id']} 的 {submitted['qty']} 件 "
                    f"{submitted['sku']} 补发申请已提交，单号 {submitted['shipment_id']}。\n"
                    "依据：全部数字来自工具记录，可逐条回溯。\n"
                    f"状态：DONE；幂等键 {submitted['idem_key']}。")
        if any(s["tool"] == "submit_request" and s["status"] == "reconciled" for s in steps):
            return "提交结果已通过对账确认。"
        return "已完成，但没有产生业务写入。"

    def _ungrounded_numbers(self, wf: str, answer: str) -> list[str]:
        """Every number in the answer must exist in a recorded tool result."""
        recorded = json.dumps(self.store.successful_results(wf), ensure_ascii=False)
        known = set(re.findall(r"\d+(?:\.\d+)?", recorded))
        seen = dict.fromkeys(re.findall(r"\d+(?:\.\d+)?", answer))
        return [n for n in seen if n not in known][:MAX_ANSWER_NUMBERS]

    # ---- guards --------------------------------------------------------
    def _over_budget(self, wf: str) -> bool:
        row = self.store.workflow(wf)
        if row["turns"] >= self.limits.max_turns:
            self._fail(wf, f"轮次超过上限 {self.limits.max_turns}")
            return True
        if time.time() - row["created"] > self.limits.max_seconds:
            self._fail(wf, f"耗时超过上限 {self.limits.max_seconds:.0f}s")
            return True
        return False

    def _fail(self, wf: str, reason: str) -> None:
        self.store.transition(wf, states.FAILED)
        self.store.event(wf, "failed", reason)
        self.on_event(Event("state", {"state": "FAILED", "reason": reason}))
