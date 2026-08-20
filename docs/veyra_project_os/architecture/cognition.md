# Cognition Architecture

## Status

`IMPLEMENTED BOUNDED ALPHA / REAL_MODEL_VALIDATED PENDING`

## 目的

认知层把分散状态组织成当前理解，并选择下一种反应。它不能通过一个新的总状态对象复制所有 truth。

## 逻辑步骤

```text
Scoped durable state
    ↓
Living Context projection
    ↓
Active Concern projection
    ↓
Situation + Known/Unknown/Assumptions
    ↓
Material change / Information Need
    ↓
Attention
    ↓
say / ask / observe / delegate / wait / silent
```

## Deterministic 与 Model 的分工

### Deterministic/server-owned

- owner/session scope；
- durable identity/revision；
- evidence provenance/freshness；
- authority ceiling；
- time/budget；
- allowlisted source；
- replay/CAS；
- Route/Risk 和副作用边界；
- final persistence/verification。

### Model-assisted

- 总结当前 Situation；
- 找出 material change；
- 提议 Information Need；
- 比较解释或建议；
- 判断表达方式和用户可理解语言；
- 在 server 提供的候选中选择相关上下文。

模型输出是 candidate，不是事实或权限。

## Information Need

当前 `Information Need` 已作为可审计的 durable bounded runtime，而不是一句
`needs_observation` disposition 后停止。它绑定 Situation、blocked judgment、
needed evidence kind、why now、expiry、allowed source class、fallback reaction
和 authority ceiling；source admission 与 lifecycle 仍由 server 控制。

最小语义包括：

- concern/situation reference；
- blocked judgment；
- needed evidence kind；
- why now；
- urgency/expiry；
- acceptable source classes；
- fallback reaction；
- authority ceiling。

具体 model、planner 类或文件结构属于可替换实现，不写入概念宪章。当前 alpha
以统一的 candidate contract 接入自然语言，不按旅行、面试、搬家分别硬编码
认知路径。

## Ask

Ask 是一种受治理的信息获取方式，不是通用聊天 fallback。

只有当答案会影响当前理解、只有用户适合回答、问题不重复且 scope 明确时才能 ask。用户回答应更新 Situation 的 Known/Unknown，而不仅成为 conversation text。

当前 reaction runtime 已覆盖 `ask`、`read`、`wait`、`silent` 和 `suggest`。
`suggest`/`ask` 的用户可见解释必须保留 what happened、why it matters、why now
和 next step；`read` 只表示受治理 source observation，而不是任意 Tool call。

## False silence

以下情况需要可观测告警：

- 有合格 material change 却长期无 Attention；
- 长期 `candidate_count=0`；
- Information Need 永久 unresolved 且没有 ask/wait/expiry；
- record-only suggestion 不断产生但没有用户可见触达；
- 因内部 degraded 把产品行为伪装成安全沉默。

## 当前实现边界

模型输出仍是 candidate/hypothesis，不是 fact、verified evidence 或 authority。
Server 负责 candidate admission、Situation revision/CAS、evidence provenance、
scope、capacity 和最终 persistence。Pre-final automated tests 覆盖这些边界；
Moonshot 的最终可复现三场景 run 尚待 root 修复剩余 blocker 后确认，因此
`REAL_MODEL_VALIDATED` 保持 `PENDING`。

## 开放问题

- Active Concern 如何在不新增总实体的情况下稳定投影；
- Situation diversity 需要几类独立证据；
- 何时应由规则、模型或混合策略发现 Information Need；
- 如何衡量 false silence 与不必要打扰；
- 如何把 event-driven 和 model brief 统一到一个产品指标体系。
