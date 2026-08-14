# Delegation Architecture

## Status

`CORE GOVERNANCE IMPLEMENTED / PRODUCT USE PARTIAL`

## 目的

Agent、Skill、Probe 和 Tool 为 Veyra 提供能力，但不拥有 Veyra 的持续上下文和最终事实权威。

## 能力分层

| 能力 | 适合处理 | 不拥有 |
|---|---|---|
| Probe | 预注册、窄、可验证的只读观察 | 开放研究与执行权 |
| Skill | 可复用工作流和来源适配 | 用户长期状态权威 |
| Agent | 开放研究、比较、规划、长链推理 | authority、最终事实和持续 Attention |
| Tool | 具体外部能力 | 为什么现在调用及结果解释 |

## 委托前

Veyra 必须知道：

- 委托绑定哪个 Concern/Situation；
- 希望回答什么问题；
- 给出哪些最小上下文；
- 哪些数据不可提供；
- 预算和截止；
- 是只读研究、提案还是现实行动；
- 如何验证结果。

## 委托后

Agent 输出默认是 reported/inferred，不是 observed/verified。

Veyra 需要：

- 保存 provenance；
- 对结构化结果进行校验；
- 将事实声明与建议分开；
- 必要时用独立来源验证；
- 明确 unknown/indeterminate；
- 更新原 Situation，而不是只保存聊天 transcript。

## 现实行动

任何写入、发送、支付、删除、发布或外部变更，需要单独 authority、review/confirmation、exact target 和结果 receipt。Information Need 或 Agent 建议不能自动升级为执行授权。

## 开放问题

- 何时 bounded Agent research 比专用 source 更合适；
- 如何避免把整个 Living Context 暴露给 Agent；
- 多 Agent 结果如何比较和保留分歧；
- 如何定义足够独立的验证来源；
- 如何将 Agent 成本纳入 Attention 和信息价值。
