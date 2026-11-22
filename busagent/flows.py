"""Known business flows: inputs, a goal contract, and a goal sentence.

A flow is data, not code. The runner only knows how to bind inputs, ask for
missing ones, and hand the contract to the verifier.

covers 声明这个流程“管到哪”；请求超出 covers 时默认拒绝，不静默执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import AgentError


@dataclass(frozen=True)
class FlowInput:
    key: str
    prompt: str
    kind: str = "text"          # text | number | enum
    required: bool = True
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowTemplate:
    flow_id: str
    title: str
    goal_template: str
    inputs: tuple[FlowInput, ...]
    contract: dict[str, Any]
    covers: tuple[str, ...] = ()


FLOWS: dict[str, FlowTemplate] = {t.flow_id: t for t in [
    FlowTemplate(
        flow_id="shipment.reship",
        title="订单缺件补发",
        goal_template="{customer_name}的订单 {order_id} 需要补发 {sku}",
        inputs=(
            FlowInput("customer_name", "客户姓名", "text"),
            FlowInput("order_id", "订单号", "number"),
            FlowInput("sku", "补发 SKU", "text"),
        ),
        contract={
            "requires": [
                {"kind": "effect", "table": "shipments",
                 "where": {"order_id": "$input:order_id", "sku": "$input:sku"},
                 "count": 1},
                {"kind": "approval"},
                {"kind": "answer", "contains": ["补发申请已提交"],
                 "contains_from_trace": ["shipment_id"]},
            ],
            "forbids": [
                {"kind": "effect", "table": "shipments",
                 "where": {"order_id": "$input:order_id"}, "count_gt": 1},
            ],
        },
        covers=("reship", "create_shipment"),
    ),
]}


# capability ids are a shared vocabulary between request text and flow covers
CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "reship": ("补发", "补寄", "缺件"),
    "create_shipment": ("发货单", "创建发货"),
    "notify_customer": ("通知客户", "告知客户", "发消息给客户"),
    "query_dataset": ("查询内容", "查询数据", "查一下内容"),
    "create_version": ("创建版本", "新建版本"),
}


@dataclass(frozen=True)
class Coverage:
    requested: tuple[str, ...]
    covered: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


class FlowCoverageError(AgentError):
    """The request asks for capabilities this flow does not cover."""

    def __init__(self, template: FlowTemplate, coverage: Coverage) -> None:
        self.template = template
        self.coverage = coverage
        self.missing = list(coverage.missing)
        super().__init__(
            f"流程 {template.flow_id} 只覆盖 {list(coverage.covered)}；"
            f"未覆盖 {list(coverage.missing)}。"
            "请用 --partial 只执行已覆盖部分，或改用通用流程。")


def requested_capabilities(text: str) -> list[str]:
    found = []
    for cap, words in CAPABILITY_KEYWORDS.items():
        if any(word in text for word in words):
            found.append(cap)
    return found


def analyze_request(text: str, template: FlowTemplate) -> Coverage:
    # V0 用关键词映射 capability；换真模型可替换这一层，差集逻辑不变。
    # 实验 8：“补发并通知客户”会命中 reship + notify_customer，后者不在 covers。
    requested = requested_capabilities(text)
    covered = tuple(cap for cap in requested if cap in template.covers)
    missing = tuple(cap for cap in requested if cap not in template.covers)
    return Coverage(tuple(requested), covered, missing)


def get_flow(flow_id: str) -> FlowTemplate:
    template = FLOWS.get(flow_id)
    if template is None:
        raise KeyError(f"未知流程 {flow_id!r}，可用：{', '.join(FLOWS)}")
    return template


def missing_inputs(template: FlowTemplate, inputs: dict[str, Any]) -> list[FlowInput]:
    return [spec for spec in template.inputs
            if spec.required and inputs.get(spec.key) in (None, "")]


def questions_for(missing: list[FlowInput]) -> list[dict[str, Any]]:
    questions = []
    for spec in missing:
        question: dict[str, Any] = {"key": spec.key, "prompt": spec.prompt,
                                    "kind": spec.kind}
        if spec.options:
            question["options"] = list(spec.options)
        questions.append(question)
    return questions


def build_goal(template: FlowTemplate, inputs: dict[str, Any]) -> str:
    return template.goal_template.format(**inputs)
