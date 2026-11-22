# 怎么读这份代码

> 别从 `cli.py` 读起。它是装配层，9 个模块里最后才该看的那个。

## 第一步：先看行为，再看代码

```bash
python3 -m busagent demo                          # 7 个场景，看它怎么失败、怎么被拦住
python3 -m busagent ask "张三的订单 1001 缺什么，帮我补发"
python3 -m busagent trace wf_xxxxx                # ← 最重要：按时间顺序看一次真实执行
```

`trace` 把一次工作流拆成三层：

1. **messages** —— 模型每轮看到了什么、提议了什么
2. **steps** —— 闸门对每次调用的判决、digest、幂等键、耗时
3. **events** —— 状态机怎么走的（wait_approval → approved → done）

先盯着 `trace` 的输出读代码，比从函数名猜意图快得多。

## 第二步：按依赖顺序读（约 20 分钟）

```
errors.py      20 行   错误类型（都是“决定”，不是崩溃）
events.py      40 行   事件流：循环只发事件，前端决定怎么展示
   ↑
contracts.py  140 行   ToolSpec / ToolCall / Message + 严格校验
   ↑
store.py      260 行   8 张表：业务 4 张 + 运行时 4 张
   ↑
tools.py      100 行   6 个业务工具 + update_plan
gate.py       125 行   ★ 核心：来源 → 审批 → 幂等 → UNKNOWN
   ↑
loop.py       210 行   ★ 主循环：每轮一次决策 + 挂起/恢复
   ↑
runtime.py    150 行   门面：start / resume / send / approve / reject / revise
session.py    200 行   外层循环：sessions × workflows，动作 reply/open/advance/status/ask
flows.py      140 行   已知流程模板：inputs / covers / contract
goal.py       160 行   五种断言的验收器（不调模型）
states.py      50 行   状态机与合法迁移
plan.py       110 行   计划提案、断言校验、合同派生
   ↑
cli.py        300 行   命令与 demo（看不看都行）
```

`models.py`（200 行）只依赖 `contracts.py`，可以最后单独看——它只是“模型”的替身。

## 第三步：照着一次请求走

以 `张三的订单 1001 缺什么，帮我补发` 为例：

| 步 | 发生什么 | 看哪里 |
|---|---|---|
| 1 | `ask` 建 workflow、写 user 消息 | `runtime.start` |
| 2 | 循环问模型：下一步做什么 | `loop._loop` |
| 3 | 模型返回 `find_customer("张三")` | `models.ScriptedModel.decide` |
| 4 | 闸门校验参数 → 执行 → 写回结果 | `gate.dispatch` → `loop._dispatch` |
| 5 | 五轮之后模型提议 `submit_request` | — |
| 6 | 写操作没有批准 → 存 pending，状态 `WAIT_APPROVAL` | `loop._dispatch` 的 `wait_approval` 分支 |
| 7 | 人批准（digest 绑定参数） | `runtime.approve` |
| 8 | `resume` 重新过闸 → 幂等键 → 提交 | `loop._resume_pending` |
| 9 | 模型收尾、答案做数字溯源 | `loop._finish` |

驳回的路径多两站：`runtime.reject` 吊销旧批准 → 模型下一轮读到「驳回：…」重新提议。

## 想改什么，看哪里

| 你想改 | 只看这一个地方 |
|---|---|
| 加一个业务工具 | `tools.py`：加 `ToolSpec` + 实现，校验和 schema 自动生效 |
| 改审批策略（免批 / 逐次批） | `gate.dispatch` |
| 换真模型 | `models.py` 的 `Model` 协议，`runtime` 不用动 |
| 加状态（比如 `CANCELLED`） | `loop.run` 的分支 + `store` 的状态字段 |
| 改幂等口径 | `gate.idem_key` |
| 改 CLI 输出 | `cli.py`，不影响任何业务逻辑 |

## 三个阅读陷阱

1. **别从 `cli.py` 读起。** 它把路径、参数、打印混在一起，读它只会得到“这个程序好像很复杂”的错觉。真正的逻辑只有 `gate.py` + `loop.py` 两个文件。
2. **`gate` 故意不读 messages。** 它只看契约、这次调用、已记录的结果。这不是遗漏——正因为不读对话，提示词注入才骗不过它，也才能用 5 条单测覆盖。
3. **状态全在数据库里。** 看到 `store.workflow(wf)` 不要以为是缓存；`resume` 每次都从库里重建，进程内存里没有隐藏状态。

## 把测试当规格读

代码注释会过期，测试不会：

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_gate.py` —— 契约、来源、审批、权限，5 条
- `tests/test_loop.py` —— 正常流、跨进程恢复、改参、驳回、吊销、超时、预算，9 条
- `tests/test_session.py` —— 会话开新任务、推进、状态、反问、旧任务保留，5 条
- `tests/test_flow.py` —— 必填追问、覆盖拒绝、部分执行、验收失败、非法迁移，6 条
- `tests/test_plan.py` —— 计划待批、越权、派生合同、驳回、空断言拒绝，7 条
- `tests/test_streaming.py` —— 流式增量、事件顺序、答案事件，3 条
- `tests/test_scenarios.py` —— 7 个 demo 场景的端到端冒烟

每条测试名就是一句需求。读完测试再读实现，会顺很多。当前共 **36 条**。
