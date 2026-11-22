"""Session layer: one conversation, many workflows.

The loop stays workflow-scoped. This module is the outer loop: a user message
enters the session log, a model picks one action (reply / open / advance /
status / ask), and the runtime executes it against the right workflow.

There is deliberately no topic classifier. Choosing a workflow is a *visible
action* the model takes, so mistakes are auditable instead of silent.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from .runtime import Runtime
from .store import Store

ACTIVE_STATES = ("RUNNING", "WAIT_APPROVAL", "UNKNOWN")


@dataclass(frozen=True)
class SessionAction:
    kind: str                 # reply | open | advance | status | ask
    text: str = ""
    wf: str = ""


@dataclass(frozen=True)
class SessionTurn:
    action: SessionAction
    reply: str
    wf: str = ""


class SessionModel(Protocol):
    def decide(self, history: Sequence[sqlite3.Row],
               workflows: Sequence[sqlite3.Row]) -> SessionAction: ...


class ScriptedSessionModel:
    """Deterministic model for tests: one queued action per user turn."""

    def __init__(self, actions: Sequence[SessionAction]) -> None:
        self._actions = list(actions)
        self._used = 0

    def decide(self, history: Sequence[sqlite3.Row],
               workflows: Sequence[sqlite3.Row]) -> SessionAction:
        if self._used >= len(self._actions):
            return SessionAction("reply", "（没有更多动作）")
        action = self._actions[self._used]
        self._used += 1
        return action


class HeuristicSessionModel:
    """V0 for the REPL: explicit commands win, otherwise continue the newest.

    This is a stand-in, not a classifier. A real model gets the same inputs
    (session history + workflow list) and returns the same SessionAction.
    """

    def decide(self, history: Sequence[sqlite3.Row],
               workflows: Sequence[sqlite3.Row]) -> SessionAction:
        text = next((row["content"] for row in reversed(history)
                     if row["role"] == "user"), "").strip()
        active = [row for row in workflows if row["state"] in ACTIVE_STATES]

        if text.startswith("/new "):
            return SessionAction("open", text[5:].strip())
        if text == "/status":
            return SessionAction("status", wf=active[-1]["id"] if active else "")
        if text.startswith("/ask "):
            return SessionAction("ask", text[5:].strip())
        if active:
            return SessionAction("advance", text, wf=active[-1]["id"])
        return SessionAction("open", text)


class SessionLoop:
    def __init__(self, store: Store, runtime: Runtime, model: SessionModel,
                 session: str, title: str = "") -> None:
        self.store = store
        self.runtime = runtime
        self.model = model
        self.session = session
        self.title = title or "新会话"
        self.store.create_session(session, self.title)

    def handle(self, text: str) -> SessionTurn:
        # 没有话题分类器：消息全进会话日志，模型返回一个可见动作。
        self.store.append_session_message(self.session, "user", text)
        history = self.store.session_messages(self.session)
        workflows = self.store.workflows_in_session(self.session)
        action = self.model.decide(history, workflows)

        reply, wf = self._execute(action, text)
        self.store.append_session_message(self.session, "assistant", reply,
                                          action=action.kind, wf=wf)
        self.store.touch_session(self.session)
        return SessionTurn(action, reply, wf)

    # ---- actions -------------------------------------------------------
    def _execute(self, action: SessionAction, fallback: str) -> tuple[str, str]:
        if action.kind == "open":
            goal = action.text or fallback
            wf = self.runtime.start(goal, session=self.session)
            return self._summary(wf, prefix="已开新 workflow"), wf

        if action.kind == "advance":
            wf = action.wf or self._newest_active()
            if not wf:
                return "当前会话没有可推进的 workflow，用 /new 开一个。", ""
            state = self.runtime.send(wf, action.text or fallback)
            return self._summary(wf, prefix=f"已交给 {wf}（{state}）"), wf

        if action.kind == "status":
            wf = action.wf or self._newest_active()
            if not wf:
                return "当前会话没有 workflow。", ""
            row = self.store.workflow(wf)
            return f"{wf} · {row['state']}\n{self.runtime.answer(wf)}", wf

        if action.kind == "ask":
            return action.text or "能再说清楚一点吗？", ""

        return action.text or "（未处理）", ""

    def _newest_active(self) -> str:
        active = [row for row in self.store.workflows_in_session(self.session)
                  if row["state"] in ACTIVE_STATES]
        return active[-1]["id"] if active else ""

    def _summary(self, wf: str, prefix: str) -> str:
        row = self.store.workflow(wf)
        return f"{prefix} · {row['state']}\n{self.runtime.answer(wf)}"


def new_session_id() -> str:
    return f"s_{uuid.uuid4().hex[:6]}"
