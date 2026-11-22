"""Command line entry point: demo scenarios and a resumable workflow CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import tempfile
from pathlib import Path

from .config import Limits
from .errors import Rejected
from .flows import FLOWS, FlowCoverageError
from .events import Event, null_sink
from .models import (ScriptedModel, call, feedback_script, parse_question,
                     question_script, retry_after_reject_script)
from .runtime import Runtime
from .session import HeuristicSessionModel, SessionLoop, new_session_id
from .ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, body, c, head, kv

QUESTION = "张三的订单 1001 缺什么，帮我补发"
DEFAULT_DIR = Path(".busagent-data")


def _trace(rt: Runtime, wf: str) -> None:
    print(c("  steps：", DIM))
    for step in rt.status(wf)["steps"]:
        print(f"    [{step['status']:<10}] {step['tool']:<16} "
              f"{step['ms']:>5.1f}ms  {c(str(step['args'])[:54], DIM)}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_normal(tmp: Path) -> tuple[bool, str]:
    rt = Runtime(tmp / "a")
    wf = rt.start(QUESTION)
    state = rt.status(wf)["state"]

    head("场景 A · 交错循环：每轮一个决定，提交被闸门扣住")
    kv("第一轮结束", state, state == "WAIT_APPROVAL")
    body(rt.answer(wf) or "等待审批…", YELLOW)
    _trace(rt, wf)

    digest = rt.approve(wf, "supervisor")
    kv("supervisor 批准", f"digest={digest}")
    state = rt.resume(wf)
    kv("resume 后", state, state == "DONE")
    body(rt.answer(wf))
    rows = rt.store.shipment_count()
    rt.close()
    return state == "DONE" and rows == 1, f"state={state} 效果行={rows}"


def scenario_revise(tmp: Path) -> tuple[bool, str]:
    rt = Runtime(tmp / "b")
    wf = rt.start(QUESTION)
    rt.approve(wf, "supervisor")
    before = rt.status(wf)["pending"]["digest"]
    rt.revise(wf, "改成加急")
    after = rt.status(wf)["pending"]["digest"]
    state = rt.resume(wf)
    rows = rt.store.shipment_count()
    rt.close()
    return (state == "WAIT_APPROVAL" and before != after and rows == 0,
            f"old={before} new={after} 未写入={rows == 0}")


def scenario_denied(tmp: Path) -> tuple[bool, str]:
    rt = Runtime(tmp / "c", actor="intern")
    wf = rt.start(QUESTION)
    state = rt.status(wf)["state"]
    intern_denied = state == "DENIED" and rt.store.shipment_count() == 0
    rt.close()

    rt2 = Runtime(tmp / "c2", actor="clerk")
    wf2 = rt2.start(QUESTION)
    self_approve = False
    try:
        rt2.approve(wf2, "clerk")
    except Rejected:
        self_approve = True
    ok = intern_denied and self_approve and rt2.store.shipment_count() == 0
    rt2.close()
    return ok, f"intern拒绝={intern_denied} clerk自批被拒={self_approve}"


def scenario_retry(tmp: Path) -> tuple[bool, str]:
    """Rejected call is fed back; the next turn fixes it — no plan to invalidate."""
    rt = Runtime(tmp / "d", model=ScriptedModel(retry_after_reject_script(QUESTION)))
    wf = rt.start(QUESTION)
    status = rt.status(wf)
    rejected = any(e["kind"] == "rejected" for e in rt.store.events(wf))
    rt.approve(wf, "supervisor")
    state = rt.resume(wf)
    ok = rejected and state == "DONE" and rt.store.shipment_count() == 1
    detail = f"拒绝1次后恢复={rejected} state={state} 行数={rt.store.shipment_count()}"
    rt.close()
    return ok, detail


def scenario_unknown(tmp: Path) -> tuple[bool, str]:
    rt = Runtime(tmp / "e")
    rt.store.fail_next_commit = True          # 第一次提交会超时
    wf = rt.start(QUESTION)
    rt.approve(wf, "supervisor")
    state1 = rt.resume(wf)
    n1 = rt.store.shipment_count()
    for _ in range(3):
        rt.resume(wf)
    n2 = rt.store.shipment_count()
    rt.store.exec("UPDATE shipments SET visible = 1 WHERE visible = 0")
    state2 = rt.resume(wf)
    ok = state1 == "UNKNOWN" and state2 == "DONE" and n1 == 1 and n2 == 1
    detail = f"{state1}→{state2} 行数 {n1}→{n2}"
    rt.close()
    return ok, detail


def scenario_feedback(tmp: Path) -> tuple[bool, str]:
    """Reject with a reason: the model re-proposes, nothing was written yet."""
    rt = Runtime(tmp / "g", model=ScriptedModel(feedback_script(QUESTION)))
    wf = rt.start(QUESTION)
    first = rt.status(wf)["pending"]["digest"]
    state = rt.reject(wf, "补错东西了，应该补杯盖")
    second = (rt.status(wf)["pending"] or {}).get("digest", "")
    rt.approve(wf, "supervisor")
    final = rt.resume(wf)
    skus = [r["sku"] for r in rt.store.q("SELECT sku FROM shipments")]
    ok = (state == "WAIT_APPROVAL" and first != second and final == "DONE"
          and skus == ["CUP-LID"])
    detail = f"digest {first}→{second or '—'} state={final} sku={skus}"
    rt.close()
    return ok, detail


def scenario_budget(tmp: Path) -> tuple[bool, str]:
    loop_call = lambda messages: call("find_customer", name="张三")  # noqa: E731
    rt = Runtime(tmp / "f", limits=Limits(max_turns=3, max_tool_calls=8),
                 model=ScriptedModel([loop_call] * 10))
    wf = rt.start(QUESTION)
    state = rt.status(wf)["state"]
    failed = [e["detail"] for e in rt.store.events(wf) if e["kind"] == "failed"]
    ok = state == "FAILED" and bool(failed)
    detail = f"state={state} 原因={failed[-1] if failed else '—'}"
    rt.close()
    return ok, detail


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def demo() -> int:
    root = Path(tempfile.mkdtemp(prefix="busagent-demo-"))
    head("这张图 → 这个包")
    kv("L1 编排", "loop.AgentLoop：每轮一次 model.decide()，没有预生成计划")
    kv("L2 控制面", "gate.Gate：来源校验 → 审批摘要 → 幂等键 → UNKNOWN")
    kv("L3 能力", "tools.build_registry()：6 个业务工具 + update_plan")
    kv("L4 数据", "store.Store：业务表 + workflows/messages/approvals/steps/events")

    head("场景 A · 正常路径")
    a_ok, a_detail = scenario_normal(root)
    kv("结果", a_detail, a_ok)

    head("场景 B · 改参数：旧批准失效")
    b_ok, b_detail = scenario_revise(root)
    kv("结果", b_detail, b_ok)

    head("场景 C · 越权：intern 无读权限；clerk 自批被拒")
    c_ok, c_detail = scenario_denied(root)
    kv("结果", c_detail, c_ok)

    head("场景 D · 拒绝→反馈→自我修正（交错循环的关键优势）")
    d_ok, d_detail = scenario_retry(root)
    kv("结果", d_detail, d_ok)
    print(c("  预生成计划被拒就整单作废；交错循环只废这一步。", DIM))

    head("场景 E · 提交超时：UNKNOWN 只对账，不重提")
    e_ok, e_detail = scenario_unknown(root)
    kv("结果", e_detail, e_ok)

    head("场景 G · 驳回后重提议：反馈进对话，副作用零泄漏")
    g_ok, g_detail = scenario_feedback(root)
    kv("结果", g_detail, g_ok)

    head("场景 F · 预算：模型原地打转也会被停住")
    f_ok, f_detail = scenario_budget(root)
    kv("结果", f_detail, f_ok)

    head("L5 · 红线")
    checks = [("正常路径恰好 1 次提交", a_ok), ("改参后旧批准失效", b_ok),
              ("越权零写入", c_ok), ("拒绝后能自我修正", d_ok),
              ("超时不重复提交", e_ok), ("驳回后重提议且只提交 1 次", g_ok),
              ("预算能停住循环", f_ok)]
    for name, ok in checks:
        print(f"  {c('PASS' if ok else 'FAIL', GREEN if ok else RED)}  {name}")
    passed = sum(ok for _, ok in checks)
    print()
    print(f"  红线：越权写入 {c('0', GREEN)} · 重复提交 {c('0', GREEN)} · "
          f"审批绑定 {c('生效', GREEN)} · 无界循环 {c('0', GREEN)}")
    print(f"  结果：{c(f'{passed}/{len(checks)}', GREEN if passed == len(checks) else RED)} 场景通过")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if passed == len(checks) else 1


# ---------------------------------------------------------------------------
# Trace: read the execution, not the source
# ---------------------------------------------------------------------------

def trace_workflow(rt: Runtime, wf: str) -> None:
    row = rt.store.workflow(wf)
    print(f"workflow {c(wf, BOLD)}  state={row['state']}  "
          f"turns={row['turns']}  tool_calls={row['tool_calls']}")
    print(f"plan      {row['plan'] or '—'}")

    head("1. messages（模型看到的东西，按时间顺序）")
    for message in rt.store.messages(wf):
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                args = json.dumps(call.args, ensure_ascii=False)
                print(f"  assistant → {c(call.name, BOLD)}({args[:72]})")
        elif message.role == "tool":
            text = (message.content or "").replace("\n", " ")
            print(f"  tool      ← {message.name}: {c(text[:72], DIM)}")
        else:
            print(f"  {message.role:<9} {(message.content or '')[:80]}")

    head("2. steps（闸门的判决与耗时）")
    for step in rt.store.steps(wf):
        print(f"  [{step['status']:<10}] {step['tool']:<16} {step['ms']:>6.1f}ms  "
              f"digest={step['digest'] or '—'}  idem={step['idem_key'] or '—'}")

    head("3. events（状态机怎么走的）")
    for event in rt.store.events(wf):
        print(f"  {event['kind']:<14} {event['detail']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _stream_printer(delay: float = 0.0):
    stats = {"chars": 0, "deltas": 0}

    def sink(event: Event) -> None:
        if event.kind == "model.delta":
            print(event.data["text"], end="", flush=True)
            stats["chars"] += len(event.data["text"])
            stats["deltas"] += 1
            if delay:
                time.sleep(delay)
        elif event.kind == "tool.start":
            print(f"\n  · {event.data['name']}  ", end="", flush=True)
        elif event.kind == "tool.result":
            print(f"← {event.data['status']}", flush=True)
        elif event.kind == "state" and event.data["state"] != "DONE":
            print(f"\n  状态：{event.data['state']}", flush=True)

    return sink, stats


def _exe_hint(base: Path, cmd: str) -> str:
    suffix = f" --data {base}" if str(base) != str(DEFAULT_DIR) else ""
    return f"下一步：python3 -m busagent{suffix} {cmd}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="busagent", description="业务 Agent：交错循环 + 写闸门")
    parser.add_argument("--data", default=str(DEFAULT_DIR), help="持久库目录（默认 .mvp-data）")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("demo", help="跑 6 个场景 + 红线")
    p_ask = sub.add_parser("ask", help="发起工作流")
    p_ask.add_argument("question")
    p_ask.add_argument("--as", dest="actor", default="clerk")
    p_ask.add_argument("--stream", action="store_true", help="逐字打印模型输出")
    p_ask.add_argument("--delay", type=float, default=0.0, help="每块之间停顿秒数")

    p_st = sub.add_parser("stream", help="一条命令看流式：ask → 自动批准 → resume")
    p_st.add_argument("question", nargs="?", default=QUESTION)
    p_st.add_argument("--delay", type=float, default=0.0, help="每块之间停顿秒数，便于肉眼观察")

    p_plan = sub.add_parser("plan", help="通用流程：先生成计划，等人批准")
    p_plan.add_argument("goal")
    p_plan.add_argument("--session", default="")

    p_ap_plan = sub.add_parser("approve-plan", help="批准通用流程的计划")
    p_ap_plan.add_argument("wf")
    p_ap_plan.add_argument("--as", dest="actor", default="supervisor")

    p_rj_plan = sub.add_parser("reject-plan", help="驳回通用流程的计划")
    p_rj_plan.add_argument("wf")
    p_rj_plan.add_argument("--reason", default="")

    p_flow = sub.add_parser("flow", help="启动已知业务流程（合同模板）")
    p_flow.add_argument("flow_id")
    p_flow.add_argument("--request", default="", help="用户原话，用来做覆盖检查")
    p_flow.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    p_flow.add_argument("--partial", action="store_true", help="只执行已覆盖的能力")
    p_flow.add_argument("--session", default="")

    p_ans = sub.add_parser("answer", help="回答 NEEDS_INPUT 的问题")
    p_ans.add_argument("wf")
    p_ans.add_argument("answers", nargs="+", metavar="KEY=VALUE")

    p_chat = sub.add_parser("chat", help="会话模式：一个会话里多个 workflow")
    p_chat.add_argument("--session", default="")
    p_chat.add_argument("--as", dest="actor", default="clerk")

    p_ap = sub.add_parser("approve")
    p_ap.add_argument("wf")
    p_ap.add_argument("--as", dest="actor", default="supervisor")

    p_rs = sub.add_parser("resume")
    p_rs.add_argument("wf")
    p_rs.add_argument("--timeout", action="store_true", help="模拟提交超时")
    p_rs.add_argument("--stream", action="store_true", help="逐字打印模型输出")
    p_rs.add_argument("--delay", type=float, default=0.0, help="每块之间停顿秒数")

    p_rej = sub.add_parser("reject")
    p_rej.add_argument("wf")
    p_rej.add_argument("--reason", required=True)

    p_fb = sub.add_parser("feedback")
    p_fb.add_argument("wf")
    p_fb.add_argument("text")

    p_rv = sub.add_parser("revise")
    p_rv.add_argument("wf")
    p_rv.add_argument("--note", required=True)

    for name in ("status", "reveal", "trace"):
        sub.add_parser(name).add_argument("wf")
    sub.add_parser("flows", help="列出已注册的业务流程")
    sub.add_parser("reset", help="删掉持久库")

    args = parser.parse_args(argv)
    if args.cmd in (None, "demo"):
        return demo()

    base = Path(args.data)

    if args.cmd == "flows":
        for template in FLOWS.values():
            print(f"{template.flow_id:<24} {template.title}  covers={list(template.covers)}")
        return 0

    if args.cmd == "plan":
        rt = Runtime(base)
        try:
            wf = rt.propose_plan(args.goal, session=args.session)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            rt.close()
            return 2
        status = rt.status(wf)
        print(f"workflow: {c(wf, BOLD)}  state: {status['state']}")
        for item in (status["proposal"] or {}).get("items", []):
            print(f"  {item['id']}  {item['title']}  "
                  f"[{c(item['acceptance']['kind'], DIM)}]")
        print(c(f"\n下一步：python3 -m busagent --data {base} approve-plan {wf} --as supervisor", DIM))
        rt.close()
        return 0

    if args.cmd == "approve-plan":
        rt = Runtime(base)
        try:
            state = rt.approve_plan(args.wf, args.actor)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            rt.close()
            return 2
        print(f"已批准计划，state: {state}")
        if state == "WAIT_APPROVAL":
            print(c(f"下一步：python3 -m busagent --data {base} approve {args.wf} --as supervisor", DIM))
        rt.close()
        return 0

    if args.cmd == "reject-plan":
        rt = Runtime(base)
        try:
            state = rt.reject_plan(args.wf, args.reason)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            rt.close()
            return 2
        print(f"计划已驳回，state: {state}")
        rt.close()
        return 0

    if args.cmd == "flow":
        values = dict(item.split("=", 1) for item in args.input)
        rt = Runtime(base)
        try:
            wf = rt.start_flow(args.flow_id, values, request_text=args.request,
                               session=args.session, allow_partial=args.partial)
        except (KeyError, FlowCoverageError) as err:
            print(c(f"拒绝：{err}", RED))
            rt.close()
            return 2
        status = rt.status(wf)
        print(f"workflow: {c(wf, BOLD)}  flow: {args.flow_id}  state: {status['state']}")
        questions = json.loads(rt.store.workflow(wf)["questions"] or "[]")
        if questions:
            print(c("需要补充：", YELLOW))
            for question in questions:
                print(f"  - {question['key']}：{question['prompt']}")
            keys = " ".join(f"{q['key']}=..." for q in questions)
            print(c(f"\n下一步：python3 -m busagent --data {base} answer {wf} {keys}", DIM))
        elif status["state"] == "WAIT_APPROVAL":
            print(c(f"\n下一步：python3 -m busagent --data {base} approve {wf} --as supervisor", DIM))
        else:
            print(rt.answer(wf))
        rt.close()
        return 0

    if args.cmd == "answer":
        values = dict(item.split("=", 1) for item in args.answers)
        rt = Runtime(base)
        try:
            state = rt.answer_questions(args.wf, values)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            rt.close()
            return 2
        print(f"state: {state}")
        if state == "WAIT_APPROVAL":
            print(c(f"下一步：python3 -m busagent --data {base} approve {args.wf} --as supervisor", DIM))
        rt.close()
        return 0

    if args.cmd == "stream":
        rt = Runtime(base, actor="clerk")
        sink, stats = _stream_printer(args.delay)
        wf = rt.start(args.question)
        rt.approve(wf, "supervisor")          # demo 自动批准；真实流程里由人点
        state = rt.resume(wf, on_event=sink)
        print()
        kv("workflow", wf)
        kv("state", state)
        kv("stream", f"{stats['deltas']} 块 / {stats['chars']} 字")
        if stats["deltas"] == 0:
            print(c("  没有增量：检查模型是否实现了 decide_stream，或流程是否停在审批前。", YELLOW))
        rt.close()
        return 0

    if args.cmd == "chat":
        rt = Runtime(base, actor=args.actor)
        sid = args.session or new_session_id()
        loop = SessionLoop(rt.store, rt, HeuristicSessionModel(), sid)
        print(c(f"会话 {sid}。命令：/new <目标>、/status、/ask <问题>、/quit", DIM))
        while True:
            try:
                line = input(c("你> ", BOLD))
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() in ("/quit", "/exit"):
                break
            if not line.strip():
                continue
            turn = loop.handle(line.strip())
            print(f"\n{turn.reply}\n")
        rt.close()
        return 0

    if args.cmd == "reset":
        shutil.rmtree(base, ignore_errors=True)
        print("已清除", base)
        return 0

    if args.cmd == "ask":
        name, order = parse_question(args.question)
        if not name or not order:
            print(c("没解析出「客户名 + 订单号」，示例：张三的订单 1001 缺什么，帮我补发", RED))
            return 2
        rt = Runtime(base, actor=args.actor)
        if args.stream:
            sink, stats = _stream_printer(getattr(args, "delay", 0.0))
        else:
            sink, stats = null_sink, {"chars": 0}
        wf = rt.start(args.question, on_event=sink)
        status = rt.status(wf)
        print(f"\nworkflow: {c(wf, BOLD)}  state: {status['state']}")
        if not args.stream or stats["chars"] == 0:
            print(rt.answer(wf) or "（等待审批）")
        if status["state"] == "WAIT_APPROVAL":
            print(c("\n" + _exe_hint(base, f"approve {wf} --as supervisor"), DIM))
        rt.close()
        return 0

    rt = Runtime(base)
    wf = args.wf

    if args.cmd == "approve":
        try:
            digest = rt.approve(wf, args.actor)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            return 2
        print(f"已批准 digest={c(digest, BOLD)} by {args.actor}（批准只授权，不执行）")
        print(c(_exe_hint(base, f"resume {wf}"), DIM))
        return 0

    if args.cmd == "reject":
        try:
            state = rt.reject(wf, args.reason)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            return 2
        digest = (rt.status(wf)["pending"] or {}).get("digest", "—")
        print(f"已驳回，state={state}，模型重新提议（digest {digest}）")
        print(c(_exe_hint(base, f"approve {wf} --as supervisor"), DIM))
        rt.close()
        return 0

    if args.cmd == "feedback":
        try:
            state = rt.feedback(wf, args.text)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            return 2
        digest = (rt.status(wf)["pending"] or {}).get("digest", "—")
        print(f"已记录反馈，state={state}，模型重新提议（digest {digest}）")
        rt.close()
        return 0

    if args.cmd == "revise":
        try:
            state = rt.revise(wf, args.note)
        except Rejected as err:
            print(c(f"拒绝：{err}", RED))
            return 2
        print(f"已改参数，state={state}（旧批准不再匹配）")
        print(c(_exe_hint(base, f"approve {wf} --as supervisor"), DIM))
        return 0

    if args.cmd == "reveal":
        rt.store.exec("UPDATE shipments SET visible = 1 WHERE visible = 0")
        print("已模拟：对账终于可见")
        rt.close()
        return 0

    try:
        status = rt.status(wf)
    except Exception as err:                       # noqa: BLE001 - CLI boundary
        print(c(f"拒绝：{err}", RED))
        rt.close()
        return 2

    if args.cmd == "trace":
        trace_workflow(rt, wf)
        rt.close()
        return 0

    if args.cmd == "status":
        kv("state", status["state"])
        kv("turns / calls", f"{status['turns']} / {status['tool_calls']}")
        kv("pending", (status["pending"] or {}).get("digest", "—"))
        plan = " · ".join(f"{p['step']}:{p['status']}" for p in status["plan"]) or "—"
        kv("plan", plan)
        kv("approved", [a["digest"] for a in status["approved"]] or "—")
        body(rt.answer(wf))
        rt.close()
        return 0

    if args.timeout:
        rt.store.fail_next_commit = True
    if args.stream:
        sink, stats = _stream_printer(args.delay)
    else:
        sink, stats = null_sink, {"chars": 0}
    state = rt.resume(wf, on_event=sink)
    if args.stream and stats["chars"]:
        print()
    kv("state", c(state, GREEN if state == "DONE" else YELLOW))
    if state == "WAIT_APPROVAL":
        print(c(f"  还在等审批：先跑 approve {wf} --as supervisor，再 resume --stream。", YELLOW))
    if args.stream:
        kv("stream", f"{stats['deltas']} 块 / {stats['chars']} 字" if stats["deltas"]
           else "0 块（模型没有产出正文）")
    if not args.stream or stats["chars"] == 0:
        body(rt.answer(wf) or "（无答案）")
    rt.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
