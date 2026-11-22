"""Tool contracts and the message vocabulary shared by loop, gate, and models.

The contract is the single source of truth: the model's JSON schema, the
gate's validation, and the grounding rules all derive from `ToolSpec`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .errors import Rejected

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass(frozen=True)
class ToolCall:
    """One model-proposed call. `id` is echoed back with the result."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """What the model returns for one turn: text, or one or more tool calls."""

    content: str | None = None
    calls: tuple[ToolCall, ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.calls)


@dataclass(frozen=True)
class Message:
    """A row of the conversation log (assistant, tool, or user)."""

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.role == "tool":
            return {"role": "tool", "tool_call_id": self.tool_call_id,
                    "name": self.name, "content": self.content or ""}
        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                    for c in self.tool_calls
                ],
            }
        return {"role": self.role, "content": self.content or ""}


@dataclass(frozen=True)
class ToolSpec:
    """One tool. `grounded` fields must appear in an earlier tool result.

    为什么 grounded 是契约的一部分：来源校验不能靠提示词自觉，
    否则模型编一个 customer_id，gate 也无从判断。
    """

    name: str
    kind: str                      # read | pure | write
    params: dict[str, type]
    required: tuple[str, ...]
    grounded: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_write(self) -> bool:
        return self.kind == "write"


def _type_ok(value: Any, want: type) -> bool:
    if want is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if want is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, want)


class ToolRegistry:
    """Contract lookup plus a strict validator. No I/O here."""

    def __init__(
        self,
        specs: Iterable[ToolSpec],
        impls: dict[str, Callable[[dict[str, Any], str], Any]],
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._impls = dict(impls)
        missing = set(self._specs) - set(self._impls)
        if missing:
            raise ValueError(f"tools without implementation: {sorted(missing)}")

    def names(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise Rejected(f"工具 {name!r} 不在契约里")
        return spec

    def impl(self, name: str) -> Callable[[dict[str, Any], str], Any]:
        return self._impls[name]

    def validate(self, call: ToolCall) -> ToolSpec:
        """Unknown tool, unknown arg, missing arg, wrong type -> Rejected."""
        spec = self.spec(call.name)
        extra = set(call.args) - set(spec.params)
        if extra:
            raise Rejected(f"{call.name}：未知参数 {sorted(extra)}")
        for name in spec.required:
            if name not in call.args:
                raise Rejected(f"{call.name}：缺少必填参数 {name}")
        for name, value in call.args.items():
            # bool 是 int 的子类；不显式排除，True 会冒充 1。
            want = spec.params[name]
            if not _type_ok(value, want):
                raise Rejected(
                    f"{call.name}.{name}：需要 {_PY_TO_JSON[want]}，"
                    f"收到 {type(value).__name__}")
        return spec

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style tool schemas, derived from the same contract."""
        return [{
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.note or spec.name,
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type": _PY_TO_JSON[v]} for k, v in spec.params.items()},
                    "required": list(spec.required),
                    "additionalProperties": False,
                },
            },
        } for spec in self._specs.values()]
