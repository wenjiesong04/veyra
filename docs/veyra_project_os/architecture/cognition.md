# Cognition Architecture

## Status

`CURRENT DIRECTION / CORE CONTRACT INCOMPLETE`

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

当前 `needs_observation` 应演进成可审计信息需求，而不是一句 disposition 后停止。

最小语义包括：

- concern/situation reference；
- blocked judgment；
- needed evidence kind；
- why now；
- urgency/expiry；
- acceptable source classes；
- fallback reaction；
- authority ceiling。

具体 Pydantic model、planner 类或文件结构属于可替换实现，不写入概念宪章。

## Ask

Ask 是一种受治理的信息获取方式，不是通用聊天 fallback。

只有当答案会影响当前理解、只有用户适合回答、问题不重复且 scope 明确时才能 ask。用户回答应更新 Situation 的 Known/Unknown，而不仅成为 conversation text。

## False silence

以下情况需要可观测告警：

- 有合格 material change 却长期无 Attention；
- 长期 `candidate_count=0`；
- Information Need 永久 unresolved 且没有 ask/wait/expiry；
- record-only suggestion 不断产生但没有用户可见触达；
- 因内部 degraded 把产品行为伪装成安全沉默。

## 开放问题

- Active Concern 如何在不新增总实体的情况下稳定投影；
- Situation diversity 需要几类独立证据；
- 何时应由规则、模型或混合策略发现 Information Need；
- 如何衡量 false silence 与不必要打扰；
- 如何把 event-driven 和 model brief 统一到一个产品指标体系。
