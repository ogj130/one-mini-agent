"""The write gate: provenance, approval binding, idempotency, UNKNOWN.

This module is the only place allowed to turn a proposal into a business
effect. It never reads the conversation; it reads the contract, the call, and
the recorded results — so a clever prompt cannot talk it into anything.

阅读顺序：先看 dispatch 的检查顺序，再看 _execute 的超时分支。
为什么 gate 不读 messages：验证不能依赖被验证者的叙述。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import ToolCall, ToolRegistry, ToolSpec
from .errors import CommitTimeout, Rejected
from .store import Store

ROLES: dict[str, set[str]] = {
    "intern": set(),
    "clerk": {"read"},
    "supervisor": {"read", "write"},
    "admin": {"read", "write"},
}


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(spec: ToolSpec, args: dict[str, Any]) -> str:
    """What an approval is bound to: tool + semantic arguments.

    为什么不是绑定“点过同意”这个动作：动作会被复用，摘要不会。
    参数改一个字，digest 就变，旧批准自动失效。
    """
    return hashlib.sha256(canonical({"tool": spec.name, "args": args}).encode()).hexdigest()[:16]


def idem_key(wf: str, spec: ToolSpec, args: dict[str, Any]) -> str:
    """Same workflow + same tool + same args = same logical operation.

    为什么带 wf：不同 workflow 的同一动作是两笔业务，不能互相去重。
    """
    return hashlib.sha256(canonical({"wf": wf, "tool": spec.name, "args": args}).encode()).hexdigest()[:16]


def _contains(value: Any, needle: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_contains(v, needle) for v in value)
    return value == needle


@dataclass(frozen=True)
class GateResult:
    status: str                    # ok | wait_approval | unknown | denied | rejected
    result: Any = None
    digest: str = ""
    idem: str = ""
    error: str = ""
    ms: float = 0.0


class Gate:
    def __init__(self, store: Store, registry: ToolRegistry, actor: str) -> None:
        self.store = store
        self.registry = registry
        self.actor = actor

    # ---- checks --------------------------------------------------------
    def _grounding(self, spec: ToolSpec, args: dict[str, Any],
                   results: Iterable[Any]) -> None:
        # 为什么查“已记录结果”而不是实时业务库：resume 要继续同一快照，
        # 否则参数会因为数据漂移而对不上批准时的摘要。
        recorded = list(results)
        for name in spec.grounded:
            if not any(_contains(r, args[name]) for r in recorded):
                raise Rejected(
                    f"{spec.name}.{name}：来源不可信 —— {args[name]!r} "
                    "没有出现在任何已记录的工具结果里")

    # ---- dispatch ------------------------------------------------------
    def dispatch(self, wf: str, call: ToolCall, results: Iterable[Any]) -> GateResult:
        # 顺序是刻意的：先证明参数合法，再证明参数有出处，最后才谈批准。
        # 实验 6：注释掉下面 is_approved 的判断，场景 B 会直接提交。
        try:
            spec = self.registry.validate(call)
        except Rejected as err:
            return GateResult("rejected", error=str(err))

        if "read" not in ROLES.get(self.actor, set()):
            return GateResult("denied", error=f"{self.actor} 没有任何读权限")

        try:
            self._grounding(spec, call.args, results)
        except Rejected as err:
            return GateResult("rejected", error=str(err))

        if spec.is_write:
            bound = digest(spec, call.args)
            # 每次 resume 都重新比对：不信任“上次批过了”这个内存结论。
            if not self.store.is_approved(wf, bound):
                return GateResult("wait_approval", digest=bound,
                                  idem=idem_key(wf, spec, call.args))
            return self._execute(wf, spec, call.args, bound)

        return self._execute(wf, spec, call.args, "")

    def _execute(self, wf: str, spec: ToolSpec, args: dict[str, Any],
                 bound: str) -> GateResult:
        idem = idem_key(wf, spec, args) if spec.is_write else ""
        payload = dict(args)
        if spec.is_write:
            # 幂等键由运行时注入，模型看不到也改不了。
            payload["_idem_key"] = idem
            payload["_fail_after_commit"] = bool(self.store.fail_next_commit)
            self.store.fail_next_commit = False

        started = time.perf_counter()
        try:
            result = self.registry.impl(spec.name)(payload, wf)
        except CommitTimeout as err:
            # 超时 ≠ 失败：业务可能已经写入，只能对账，不能重提。
            return GateResult("unknown", digest=bound, idem=idem, error=str(err))
        except Exception as err:                      # business error -> readable to model
            return GateResult("rejected", digest=bound, idem=idem,
                              error=f"{type(err).__name__}: {err}")
        elapsed = (time.perf_counter() - started) * 1000
        return GateResult("ok", result=result, digest=bound, idem=idem, ms=elapsed)

    # ---- recovery ------------------------------------------------------
    def reconcile(self, idem: str) -> dict[str, Any] | None:
        """Only reads. Never re-submits, never guesses.

        实验 4/5：resume --timeout 后连续 resume，这一行只查不写。
        """
        return self.store.find_shipment(idem)
