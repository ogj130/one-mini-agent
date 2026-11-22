"""Workflow state machine.

Transitions are explicit and validated in one place. Every state name here is
also stored in `workflows.state`, so the loop, the flow runner, and the CLI all
speak the same vocabulary.

为什么集中：状态散落在各处写，终态被重启这类 bug 迟早出现。
实验：把 DONE→RUNNING 加进 _ALLOWED，test_terminal_state_cannot_restart 会变红。
"""

from __future__ import annotations

from .errors import AgentError


class InvalidTransition(AgentError):
    """Attempted a transition the state machine does not allow."""


NEW = "NEW"
RUNNING = "RUNNING"
NEEDS_INPUT = "NEEDS_INPUT"
WAIT_APPROVAL = "WAIT_APPROVAL"
WAIT_PLAN_APPROVAL = "WAIT_PLAN_APPROVAL"
UNKNOWN = "UNKNOWN"
DONE = "DONE"
DENIED = "DENIED"
FAILED = "FAILED"
NEEDS_REVIEW = "NEEDS_REVIEW"

TERMINAL = {DONE, DENIED, FAILED, NEEDS_REVIEW}

# 只列出“允许去哪里”；没列出的迁移一律抛 InvalidTransition。
_ALLOWED: dict[str, set[str]] = {
    NEW: {RUNNING, NEEDS_INPUT, WAIT_PLAN_APPROVAL, DENIED},
    RUNNING: {WAIT_APPROVAL, UNKNOWN, NEEDS_INPUT, DONE, DENIED, FAILED, NEEDS_REVIEW},
    NEEDS_INPUT: {RUNNING, DENIED},
    WAIT_APPROVAL: {RUNNING, WAIT_APPROVAL, UNKNOWN, DENIED, NEEDS_INPUT},
    WAIT_PLAN_APPROVAL: {RUNNING, DENIED},
    UNKNOWN: {UNKNOWN, RUNNING, NEEDS_REVIEW},
    DONE: set(),
    DENIED: set(),
    FAILED: set(),
    NEEDS_REVIEW: set(),
}


def can_transition(old: str, new: str) -> bool:
    return new in _ALLOWED.get(old, set())


def ensure_transition(old: str, new: str) -> None:
    if not can_transition(old, new):
        raise InvalidTransition(f"{old} → {new} 不是合法迁移")
