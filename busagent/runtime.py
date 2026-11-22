"""Facade used by the CLI and tests: one workflow at a time, no hidden state."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .config import Limits
from .contracts import Message
from .errors import Rejected
from .events import EventSink, null_sink
from .flows import (FlowCoverageError, analyze_request, build_goal, get_flow,
                    missing_inputs, questions_for)
from .gate import ROLES, digest as digest_of, idem_key
from .loop import AgentLoop
from .models import Model, ScriptedModel, question_script
from .plan import PlanProposal, Planner, ScriptedPlanner, derive_contract
from . import states
from .store import Store
from .tools import build_registry


class Runtime:
    def __init__(self, data_dir: Path | str, actor: str = "clerk",
                 limits: Limits | None = None, model: Model | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.actor = actor
        self.limits = limits or Limits()
        self.store = Store(self.data_dir / "agent.db")
        self.registry = build_registry(self.store)
        self._model = model

    def close(self) -> None:
        self.store.close()

    def model_for(self, wf: str, goal: str) -> Model:
        if self._model is not None:
            return self._model
        row = self.store.workflow(wf)
        inputs = json.loads(row["inputs"] or "{}")
        return ScriptedModel(question_script(goal, preferred_sku=inputs.get("sku")))

    def _loop(self, wf: str, goal: str, on_event: EventSink = null_sink) -> AgentLoop:
        return AgentLoop(self.store, self.registry, self.model_for(wf, goal),
                         self.limits, self.actor, on_event)

    # ---- lifecycle -----------------------------------------------------
    def start(self, goal: str, on_event: EventSink = null_sink,
              session: str = "") -> str:
        wf = f"wf_{uuid.uuid4().hex[:5]}"
        self.store.create_workflow(wf, goal, self.actor, session)
        self.store.append_message(wf, Message(role="user", content=goal))
        self.store.event(wf, "created", f"actor={self.actor}")
        self.store.transition(wf, states.RUNNING)
        self._loop(wf, goal, on_event).run(wf)
        return wf

    # ---- generic plan --------------------------------------------------
    def propose_plan(self, goal: str, session: str = "",
                     planner: Planner | None = None) -> str:
        proposal = (planner or ScriptedPlanner()).propose(goal)
        derive_contract(proposal)          # fail early on unverifiable items
        wf = f"wf_{uuid.uuid4().hex[:5]}"
        self.store.create_workflow(wf, goal, self.actor, session)
        self.store.append_message(wf, Message(role="user", content=goal))
        self.store.set_proposal(wf, json.dumps(proposal.to_dict(), ensure_ascii=False))
        self.store.transition(wf, states.WAIT_PLAN_APPROVAL)
        self.store.event(wf, "plan_proposed", f"items={len(proposal.items)}")
        return wf

    def approve_plan(self, wf: str, approver: str,
                     on_event: EventSink = null_sink) -> str:
        if "write" not in ROLES.get(approver, set()):
            raise Rejected(f"{approver} 没有计划审批权")
        row = self.store.workflow(wf)
        if row["state"] != states.WAIT_PLAN_APPROVAL:
            raise Rejected(f"{wf} 当前是 {row['state']}，没有待批计划")
        # 批准的是计划；合同由计划派生，不是模型在收尾时另写一份。
        proposal = PlanProposal.from_dict(json.loads(row["proposal"] or "{}"))
        contract = derive_contract(proposal)
        self.store.set_contract(wf, json.dumps(contract, ensure_ascii=False))
        self.store.transition(wf, states.RUNNING)
        self.store.event(wf, "plan_approved", f"by={approver}")
        return self._loop(wf, row["goal"], on_event).run(wf)

    def reject_plan(self, wf: str, reason: str) -> str:
        row = self.store.workflow(wf)
        if row["state"] != states.WAIT_PLAN_APPROVAL:
            raise Rejected(f"{wf} 当前是 {row['state']}，没有待批计划")
        self.store.transition(wf, states.DENIED)
        self.store.event(wf, "plan_rejected", reason[:200])
        return states.DENIED

    # ---- known flow ----------------------------------------------------
    def start_flow(self, flow_id: str, inputs: dict[str, Any], request_text: str = "",
                   session: str = "", allow_partial: bool = False,
                   on_event: EventSink = null_sink) -> str:
        # 默认严格：请求超出 covers 直接拒绝；--partial 才允许只做已覆盖部分。
        template = get_flow(flow_id)
        coverage = analyze_request(request_text, template)
        if coverage.missing and not allow_partial:
            raise FlowCoverageError(template, coverage)

        wf = f"wf_{uuid.uuid4().hex[:5]}"
        goal = request_text or template.title
        self.store.create_workflow(wf, goal, self.actor, session)
        self.store.set_flow(wf, flow_id,
                            json.dumps(template.contract, ensure_ascii=False),
                            json.dumps(inputs, ensure_ascii=False))
        self.store.set_coverage(wf, json.dumps({
            "requested": list(coverage.requested),
            "covered": list(coverage.covered),
            "missing": list(coverage.missing),
        }, ensure_ascii=False))
        if request_text:
            self.store.append_message(wf, Message(role="user", content=request_text))
        self.store.event(wf, "flow_started", flow_id)

        missing = missing_inputs(template, inputs)
        if missing:
            self.store.set_questions(wf, json.dumps(questions_for(missing), ensure_ascii=False))
            self.store.transition(wf, states.NEEDS_INPUT)
            return wf

        self._run_flow(wf, template, inputs, on_event)
        return wf

    def answer_questions(self, wf: str, answers: dict[str, Any],
                         on_event: EventSink = null_sink) -> str:
        row = self.store.workflow(wf)
        if row["state"] != states.NEEDS_INPUT:
            raise Rejected(f"{wf} 当前是 {row['state']}，不在等补充信息")
        template = get_flow(row["flow_id"])
        inputs = json.loads(row["inputs"] or "{}")
        inputs.update(self._coerce(template, answers))
        self.store.update_workflow(wf, inputs=json.dumps(inputs, ensure_ascii=False))
        self.store.append_message(wf, Message(
            role="user", content="补充信息：" + json.dumps(answers, ensure_ascii=False)))

        missing = missing_inputs(template, inputs)
        if missing:
            self.store.set_questions(wf, json.dumps(questions_for(missing), ensure_ascii=False))
            return states.NEEDS_INPUT
        self.store.set_questions(wf, "[]")
        self.store.transition(wf, states.RUNNING)
        return self._run_flow(wf, template, inputs, on_event)

    def _run_flow(self, wf: str, template: Any, inputs: dict[str, Any],
                  on_event: EventSink) -> str:
        if self.store.workflow(wf)["state"] == states.NEW:
            self.store.transition(wf, states.RUNNING)
        goal = build_goal(template, inputs)
        self.store.update_workflow(wf, goal=goal)
        if not self.store.messages(wf):
            self.store.append_message(wf, Message(role="user", content=goal))
        self.store.event(wf, "flow_running", template.flow_id)
        return self._loop(wf, goal, on_event).run(wf)

    @staticmethod
    def _coerce(template: Any, answers: dict[str, Any]) -> dict[str, Any]:
        typed: dict[str, Any] = {}
        for spec in template.inputs:
            if spec.key not in answers:
                continue
            value = answers[spec.key]
            typed[spec.key] = int(value) if spec.kind == "number" and str(value).isdigit() else value
        return typed

    def resume(self, wf: str, on_event: EventSink = null_sink) -> str:
        goal = self.store.workflow(wf)["goal"]
        return self._loop(wf, goal, on_event).run(wf)

    def send(self, wf: str, text: str, on_event: EventSink = null_sink) -> str:
        """把一条用户消息交给某个 workflow：等审批时等同 feedback，其余追加后续跑。"""
        row = self.store.workflow(wf)
        if row["state"] in states.TERMINAL:
            raise Rejected(f"{wf} 已结束（{row['state']}），请新开 workflow")
        if row["state"] == states.NEEDS_INPUT:
            raise Rejected(f"{wf} 还在等补充信息，请用 answer_questions")
        if row["state"] == "WAIT_APPROVAL":
            self._drop_pending(wf, text, {"error": "用户补充，未执行"})
        else:
            self.store.append_message(wf, Message(role="user", content=text))
            self.store.event(wf, "user_message", text[:200])
        return self.resume(wf, on_event)

    # ---- human channel -------------------------------------------------
    def approve(self, wf: str, approver: str) -> str:
        if "write" not in ROLES.get(approver, set()):
            raise Rejected(f"{approver} 没有审批权")
        pending = self._pending(wf)
        if not pending:
            raise Rejected("当前没有待批准的动作")
        self.store.approve(wf, pending["digest"], approver)
        self.store.event(wf, "approved", f"by={approver} digest={pending['digest']}")
        return pending["digest"]

    def reject(self, wf: str, reason: str) -> str:
        """Human says no, with a reason. The reason enters the conversation
        and the model proposes again — this is the feedback loop, not a restart."""
        self._drop_pending(wf, f"驳回：{reason}", {"error": "用户驳回，未执行"})
        return self.resume(wf)

    def feedback(self, wf: str, text: str) -> str:
        """Free-form steering at the approval boundary."""
        self._drop_pending(wf, f"用户反馈：{text}", {"error": "用户要求修改，未执行"})
        return self.resume(wf)

    def _drop_pending(self, wf: str, content: str, tool_payload: dict[str, Any]) -> None:
        row = self.store.workflow(wf)
        if row["state"] != "WAIT_APPROVAL":
            raise Rejected(f"工作流当前是 {row['state']}，没有可反馈的动作")
        pending = self._pending(wf) or {}
        call = pending.get("call") or {}
        self.store.append_message(wf, Message(role="user", content=content))
        if call:
            self.store.append_message(wf, Message(
                role="tool", tool_call_id=call.get("id"), name=call.get("name"),
                content=json.dumps(tool_payload, ensure_ascii=False)))
        # 用户明确驳回 = 之前所有批准作废，不能靠旧 digest 复活
        self.store.exec("UPDATE approvals SET active = 0 WHERE wf = ?", (wf,))
        self.store.transition(wf, states.RUNNING, pending=None)
        self.store.event(wf, "user_feedback", content[:200])

    def revise(self, wf: str, note: str) -> str:
        """Change the pending call: the old approval no longer matches."""
        pending = self._pending(wf)
        if not pending:
            raise Rejected("当前没有待修改的动作")
        call = pending["call"]
        spec = self.registry.spec(call["name"])
        call["args"]["note"] = note
        pending["digest"] = digest_of(spec, call["args"])
        pending["idem"] = idem_key(wf, spec, call["args"])
        self.store.transition(wf, states.WAIT_APPROVAL,
                              pending=json.dumps(pending, ensure_ascii=False))
        self.store.event(wf, "revised", f"note={note} new_digest={pending['digest']}")
        return "WAIT_APPROVAL"

    def _pending(self, wf: str) -> dict[str, Any] | None:
        raw = self.store.workflow(wf)["pending"]
        return json.loads(raw) if raw else None

    # ---- read models ---------------------------------------------------
    def status(self, wf: str) -> dict[str, Any]:
        row = self.store.workflow(wf)
        pending = self._pending(wf)
        return {
            "wf": wf,
            "state": row["state"],
            "turns": row["turns"],
            "tool_calls": row["tool_calls"],
            "pending": pending,
            "plan": json.loads(row["plan"] or "[]"),
            "proposal": json.loads(row["proposal"] or "null"),
            "approved": [dict(r) for r in self.store.approvals(wf)],
            "steps": [dict(r) for r in self.store.steps(wf)],
            "answer": row["answer"] or "",
            "shipments": self.store.shipment_count(),
        }

    def answer(self, wf: str) -> str:
        row = self.store.workflow(wf)
        if row["answer"]:
            return row["answer"]
        state = row["state"]
        if state == "WAIT_APPROVAL":
            pending = self._pending(wf) or {}
            return (f"等待审批（digest {pending.get('digest', '—')}）。"
                    "批准后 resume 才会真正提交。")
        if state == "UNKNOWN":
            return "提交结果未知：可能已写入，但对账暂不可见。只查不重提。"
        if state == "DENIED":
            return "越权，未执行；没有任何业务写入。"
        if state == "FAILED":
            return "已停止：超出预算或工具连续失败。"
        return "运行中。"
