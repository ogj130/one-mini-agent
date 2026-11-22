# 学习型 MVP：范围与实验

> 这是一个**用来学架构**的最小业务 Agent，不是生产系统。
> 目标不是“功能多”，而是“每个概念都能被跑出来、被改坏、再被解释清楚”。

## MVP 的定义

> 一条用户消息进入会话，被路由到某个业务目标；内层循环每轮只决定下一步；
> 所有写操作必须过闸；收尾必须由合同验收；全过程可恢复、可回放。

做到这五件事，MVP 就算成立。其余都是加分项。

## 学什么（概念 → 代码 → 可观察的现象）

| 概念 | 看这里 | 跑出来是什么样 |
|---|---|---|
| 会话层选目标 | `session.py` | 一个 session 里两个 workflow 并存 |
| 计划是工件，不驱动执行 | `tools.py::update_plan` | 把 plan 改成 blocked，执行照常 |
| 交错循环 | `loop.py::_loop` | `trace` 里上一轮结果决定下一轮参数 |
| 写闸门 | `gate.py::dispatch` | 提交停在 `WAIT_APPROVAL`，digest 绑定 |
| 幂等 | `gate.py::idem_key` | 超时后 resume 多次，业务行仍 1 条 |
| UNKNOWN 对账 | `loop.py::_reconcile` | 只查不重提，`reveal` 后才 DONE |
| 目标验收 | `goal.py` | 删掉发货行，`verify_goal` 立刻缺 effect |
| 状态机 | `states.py` | 终态再 resume 直接返回，不重启 |
| 计划批准 | `plan.py` | 通用流程停在 `WAIT_PLAN_APPROVAL`，驳回即 DENIED |
| 覆盖检查 | `flows.py::analyze_request` | “补发并通知客户”被拒，`--partial` 才执行并注明未覆盖 |

## 不做什么（刻意留白）

- 不接真模型：`ScriptedModel` 是确定性替身，换模型只改一个类
- 不做多租户、并发租约、上下文压缩、重试退避、OTel
- 不做审批 UI、登录、权限管理；`--as` 只是演示
- 不接真实业务系统；SQLite fixture 足够

这些都属于“生产化”，不是“学架构”的必需项。

## 45 分钟实验清单

按顺序做，每一步都先猜结果，再看输出。

| # | 操作 | 预期现象 | 学到什么 |
|---|---|---|---|
| 1 | `python3 -m busagent demo` | 7/7 通过 | 基线 |
| 2 | `python3 -m busagent stream "..." --delay 0.08` | 逐块输出，9 块 | 事件流与前端解耦 |
| 3 | `python3 -m busagent reject wf_x --reason "换成杯盖"` | digest 变、旧批准失效 | 反馈是对话，不是重开 |
| 4 | `resume wf_x --timeout` 再连续 resume | 一直 UNKNOWN，行数不变 | 超时 ≠ 失败 |
| 5 | `reveal wf_x` 后 resume | DONE，仍 1 条 | 只对账不重提 |
| 6 | 注释 `gate.py` 里 digest 比较 | 场景 B 失败 / 直接提交 | 闸门那一行就是核心 |
| 7 | 删除 `shipments` 某行后跑 `verify_goal` | missing: effect … | 完成不能靠模型自述 |
| 8 | `flow ... --request "补发并通知客户"` | 拒绝并列出 notify_customer | 部分命中不静默执行 |
| 9 | `plan "..."` → `reject-plan wf_x` | DENIED，零写入 | 计划是粗闸门 |
| 10 | 把 `plan.py` 某个 acceptance 删掉再 `plan` | `derive_contract` 拒绝 | 计划项必须可验收 |

## 判定“学会了”的五个问题

能不看代码回答出来，MVP 的目的就达到了：

1. 为什么 `ask` 停在 `WAIT_APPROVAL` 时没有流式输出？
2. 为什么 resume 不需要恢复 Python 调用栈？
3. 为什么批准绑定 digest 而不是绑定“同意”这个动作？
4. 为什么 `UNKNOWN` 不能当失败重试？
5. 为什么计划不能由模型在收尾时自己写合同？

## 代码里的注释约定

只写三类，其余不写：

- `# 为什么：……` —— 解释非直觉的设计选择，不翻译代码
- `# 实验 N：……` —— 指向本文实验清单，方便边读边改
- 顺序/边界说明 —— 比如“先存 pending 再改状态”

判断标准：**删掉这条注释，读者会不会误解代码？** 不会，就不写。
注释和代码冲突时，以代码为准；改行为时顺手改注释。

## 怎么读代码

见 [READING.md](READING.md)：先跑 `trace`，再按 `contracts → store → gate → loop → runtime` 的顺序读，测试当规格。
