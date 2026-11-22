"""Model adapters.

`ScriptedModel` is a stand-in for a real LLM: it returns one decision per turn
and *reads the previous tool results*, which is what makes the interleaved
shape testable offline. The index is derived from the conversation log, so a
workflow resumed in a new process continues where it stopped.

Swap in `OpenAICompatibleModel` for a real provider; nothing else changes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from typing import Any, Callable, Protocol, Sequence

from .contracts import Decision, Message, ToolCall

StepFn = Callable[[Sequence[Message]], Decision]


def call(tool: str, /, **args: Any) -> Decision:
    """One-turn decision: invoke a single tool (tool name is positional-only)."""
    return Decision(calls=(ToolCall(id=f"c_{uuid.uuid4().hex[:6]}", name=tool, args=args),))


def last_result(messages: Sequence[Message], tool: str) -> Any:
    """Read the most recent successful result for a tool, from the log."""
    for message in reversed(messages):
        if message.role == "tool" and message.name == tool and message.content:
            try:
                return json.loads(message.content)
            except json.JSONDecodeError:
                return None
    return None


class Model(Protocol):
    def decide(self, messages: Sequence[Message],
               tools: list[dict[str, Any]]) -> Decision: ...

    def decide_stream(self, messages: Sequence[Message],
                      tools: list[dict[str, Any]],
                      on_delta: Callable[[str], None]) -> Decision: ...


class BaseModel:
    """Shared streaming behaviour: chunk the final text, keep the decision intact."""

    chunk_size = 12

    def decide_stream(self, messages: Sequence[Message],
                      tools: list[dict[str, Any]],
                      on_delta: Callable[[str], None]) -> Decision:
        decision = self.decide(messages, tools)
        if decision.content:
            for start in range(0, len(decision.content), self.chunk_size):
                on_delta(decision.content[start:start + self.chunk_size])
        return decision


class ScriptedModel(BaseModel):
    """Deterministic model: the Nth turn runs `steps[N]`.

    A step sees the whole message list, so it can react to earlier results —
    e.g. pick the customer id returned by `find_customer` instead of guessing.
    """

    def __init__(self, steps: Sequence[StepFn]) -> None:
        self._steps = list(steps)

    @property
    def steps(self) -> int:
        return len(self._steps)

    def decide(self, messages: Sequence[Message],
               tools: list[dict[str, Any]]) -> Decision:
        # 不用内部计数器：assistant 消息条数就是“做到第几步”，
        # 这样新进程 resume 时也能推导出同样的位置。
        used = sum(1 for m in messages if m.role == "assistant")
        if used >= len(self._steps):
            return Decision(content=compose_answer(messages))
        return self._steps[used](messages)


class OpenAICompatibleModel(BaseModel):
    """Minimal client for any /chat/completions endpoint (stdlib only)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def decide(self, messages: Sequence[Message],
               tools: list[dict[str, Any]]) -> Decision:
        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        message = body["choices"][0]["message"]
        calls = tuple(
            ToolCall(id=c["id"], name=c["function"]["name"],
                     args=json.loads(c["function"]["arguments"] or "{}"))
            for c in message.get("tool_calls") or []
        )
        return Decision(content=message.get("content") or None, calls=calls)

    def decide_stream(self, messages: Sequence[Message],
                      tools: list[dict[str, Any]],
                      on_delta: Callable[[str], None]) -> Decision:
        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
            "stream": True,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    on_delta(delta["content"])
                for part in delta.get("tool_calls") or []:
                    index = int(part.get("index", 0))
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = part.get("id") or slot["id"]
                    function = part.get("function") or {}
                    slot["name"] = function.get("name") or slot["name"]
                    slot["arguments"] += function.get("arguments") or ""
        tool_calls = tuple(
            ToolCall(id=slot["id"] or f"c_{i}", name=slot["name"],
                     args=json.loads(slot["arguments"] or "{}"))
            for i, slot in sorted(calls.items())
        )
        return Decision(content="".join(text_parts) or None, calls=tool_calls)

    @classmethod
    def from_env(cls) -> "OpenAICompatibleModel":
        base = os.environ.get("BUSAGENT_BASE_URL", "http://localhost:8788/v1")
        key = os.environ.get("BUSAGENT_API_KEY", "sk-local")
        model = os.environ.get("BUSAGENT_MODEL", "deepseek-v4.1-flash")
        return cls(base, key, model)


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

def feedback_after_submit(messages: Sequence[Message]) -> list[str]:
    """User messages that arrived after the last submit proposal."""
    last = -1
    for index, message in enumerate(messages):
        if message.role == "assistant" and any(c.name == "submit_request" for c in message.tool_calls):
            last = index
    if last < 0:
        return []
    return [m.content or "" for m in messages[last + 1:] if m.role == "user"]


def compose_answer(messages: Sequence[Message]) -> str | None:
    """Stand-in for the model's final message: numbers taken from tool results."""
    for message in reversed(messages):
        if message.role != "tool" or message.name != "submit_request" or not message.content:
            continue
        try:
            data = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if "shipment_id" not in data:
            continue
        return (f"结论：订单 {data['order_id']} 的 {data['qty']} 件 {data['sku']} "
                f"补发申请已提交，单号 {data['shipment_id']}。\n"
                "依据：全部数字来自工具记录，可逐条回溯。\n"
                f"状态：DONE；幂等键 {data['idem_key']}。")
    return None


def _customer_name(question: str) -> str:
    for pattern in (r"([\u4e00-\u9fa5]{2,3})的", r"([\u4e00-\u9fa5]{2,3})订单"):
        match = re.search(pattern, question)
        if match:
            return match.group(1)
    return ""


def _order_id(question: str) -> int | None:
    match = re.search(r"(\d{4})", question)
    return int(match.group(1)) if match else None


def parse_question(question: str) -> tuple[str, int | None]:
    return _customer_name(question), _order_id(question)


def question_script(question: str, preferred_sku: str | None = None) -> list[StepFn]:
    """The 'model' for `ask`: adaptive turns, each reading prior results."""
    name, order = parse_question(question)

    def plan_start(messages: Sequence[Message]) -> Decision:
        return call("update_plan", step="核对缺件并补发", status="in_progress")

    def ask_name(messages: Sequence[Message]) -> Decision:
        return call("find_customer", name=name)

    def ask_order(messages: Sequence[Message]) -> Decision:
        found = last_result(messages, "find_customer") or {}
        candidates = found.get("candidates") or []
        if not candidates:
            return Decision(content="找不到这个客户，请确认名字。")
        return call("get_order", customer_id=candidates[0]["id"], order_id=order)

    def ask_missing(messages: Sequence[Message]) -> Decision:
        order_row = last_result(messages, "get_order") or {}
        return call("get_missing", order_id=order_row["order_id"])

    def ask_draft(messages: Sequence[Message]) -> Decision:
        order_row = last_result(messages, "get_order") or {}
        missing = (last_result(messages, "get_missing") or {}).get("missing") or []
        if not missing:
            return Decision(content="这个订单没有缺件记录。")
        item = missing[0]
        return call("draft_request", order_id=order_row["order_id"],
                    sku=item["sku"], qty=item["qty"], note="客户反馈缺件")

    def ask_submit(messages: Sequence[Message]) -> Decision:
        draft = (last_result(messages, "draft_request") or {}).get("draft") or {}
        return call("submit_request", order_id=draft["order_id"], sku=draft["sku"],
                    qty=draft["qty"], note=draft["note"])

    def after_submit(messages: Sequence[Message]) -> Decision:
        # 提交之后只要又出现了用户消息，就按反馈重新提议，而不是结束任务
        notes = feedback_after_submit(messages)
        if notes:
            want_lid = any("杯盖" in n for n in notes)
            order_row = last_result(messages, "get_order") or {}
            items = (last_result(messages, "get_missing") or {}).get("missing") or []
            chosen = next((i for i in items if (i["sku"] == "CUP-LID") == want_lid), items[0])
            return call("submit_request", order_id=order_row["order_id"], sku=chosen["sku"],
                        qty=chosen["qty"],
                        note="按用户反馈改为杯盖" if want_lid else "客户反馈缺件")
        return call("update_plan", step="核对缺件并补发", status="done")

    def finish(messages: Sequence[Message]) -> Decision:
        last = last_result(messages, "update_plan") or {}
        if any(i.get("status") == "done" for i in last.get("plan") or []):
            return Decision(content=compose_answer(messages))
        return call("update_plan", step="核对缺件并补发", status="done")

    # 计划是循环里的工件：开始标 in_progress，结束后标 done，但它不拥有执行权
    return [plan_start, ask_name, ask_order, ask_missing, ask_draft, ask_submit,
            after_submit, finish]


def feedback_script(question: str) -> list[StepFn]:
    """Alias: the default script already reacts to user feedback."""
    return question_script(question)


def bad_source_script(question: str) -> list[StepFn]:
    """A model that fabricates a customer id instead of reading the result."""
    name = _customer_name(question)
    order = _order_id(question)

    def first(messages: Sequence[Message]) -> Decision:
        return call("find_customer", name=name)

    def fabricate(messages: Sequence[Message]) -> Decision:
        return call("get_order", customer_id=2, order_id=order)   # wrong on purpose

    return [first, fabricate]


def retry_after_reject_script(question: str) -> list[StepFn]:
    """Bad call, rejection fed back, then a corrected run — the loop's advantage."""
    name, order = parse_question(question)

    def first(messages: Sequence[Message]) -> Decision:
        return call("find_customer", name=name)

    def fabricate(messages: Sequence[Message]) -> Decision:
        return call("get_order", customer_id=2, order_id=order)

    def corrected(messages: Sequence[Message]) -> Decision:
        candidates = (last_result(messages, "find_customer") or {}).get("candidates") or []
        return call("get_order", customer_id=candidates[0]["id"], order_id=order)

    def missing(messages: Sequence[Message]) -> Decision:
        order_row = last_result(messages, "get_order") or {}
        return call("get_missing", order_id=order_row["order_id"])

    def draft(messages: Sequence[Message]) -> Decision:
        order_row = last_result(messages, "get_order") or {}
        item = ((last_result(messages, "get_missing") or {}).get("missing") or [{}])[0]
        return call("draft_request", order_id=order_row["order_id"], sku=item["sku"],
                    qty=item["qty"], note="客户反馈缺件")

    def submit(messages: Sequence[Message]) -> Decision:
        d = (last_result(messages, "draft_request") or {}).get("draft") or {}
        return call("submit_request", order_id=d["order_id"], sku=d["sku"],
                    qty=d["qty"], note=d["note"])

    return [first, fabricate, corrected, missing, draft, submit]
