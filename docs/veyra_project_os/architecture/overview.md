# Architecture Overview

## Status

`CURRENT APPROACH / IMPLEMENTED BOUNDED ALPHA / LIVE VALIDATION PENDING`

## 目标结构

```text
User input / Time / Trusted sources / Outcomes
                      ↓
                 Event Fabric
                      ↓
        Evidence + Belief + Durable State
                      ↓
       Living Context (logical projection)
                      ↓
       Active Concern (logical projection)
                      ↓
              Situation / Unknown
                      ↓
          Attention / Information Need
                 ↙               ↘
        Reaction choice       Observation
        say/ask/wait/...      source/Agent/user
                 ↘               ↙
             Outcome / Verification
                      ↓
              Feedback / Calibration
```

## 当前已有基础

当前代码已经拥有可复用的前台消息链、WorldState、Goal/Commitment、Belief/Evidence、EventInbox、Situation、AttentionHypothesis、Suggestion、Agent/Tool 治理、Verifier 和后台 Active Loop。

这些能力不应被重新实现。下一阶段的核心是让它们围绕真实 Active Concern 形成一条用户可感知纵向闭环。

Workspace Observer 是可信观察链的 Developer canary，也是 Self State 的一个来源；它不是产品身份，也不能单独证明真实生活认知已经完成。

## 2026-08-20 V1 alpha implementation alignment

当前实现已经把一条受治理的纵向链路接通：用户自然语言进入候选
Living Context，server 按 exact owner/session 与 Situation revision admission，
再把 durable Known/Unknown/Assumptions/timeline/evidence/Information Need
投影为 Situation 和 reaction。三类生活情境共用同一条语义路径，不为每个场景
维护独立硬编码流程。

当前 source capability 只包含 Calendar、Weather 和 Public Web 的只读受治理
类别（以及用户回答和本地受限观察）；每个 source 受 scope、consent、freshness、
TTL、预算和 typed receipt 约束。模型可以提出候选和 Information Need，但不能
自由生成 source 参数，也不能开启 Agent research、外部 delivery 或执行权限。

当前 reaction runtime 记录 `ask/read/wait/silent/suggest`，并将解释性字段和
feedback effect 留在同一 exact Situation scope。反馈后效只允许 timing、cooldown
和 suppression；Route/Risk、Agent、Tool/Grant 和 external delivery 不变。

上述路径有 pre-final automated evidence；Moonshot 最终可复现 run、clean
runtime/live、浏览器和 exact-SHA CI 仍是独立 `PENDING` 位置。

## 逻辑概念与工程实体

下列概念默认是 projection，不要求同名实体：

- Living Context；
- Active Concern；
- “当前用户生活摘要”；
- “为什么现在”；
- “等待中的信息”。

现有 Goal、Commitment、Situation、Belief、Attention 等仍保留各自权威存储和生命周期。

## 可替换部分

- 模型/provider；
- Agent runtime；
- source adapter；
- scheduler 算法；
- UI 框架；
- projection 实现；
- ranking/calibration 算法；
- JSON/数据库等持久化技术。

## 不可协商的不变量

- exact owner/session scope；
- provenance、freshness、conflict；
- GET 纯读；
- 模型不能自授 authority；
- 外部副作用必须有 server-owned authorization 与 receipt；
- 结果验证和 indeterminate 语义；
- 用户可见、可纠正、可停止；
- record-only 证据不能冒充已交付用户价值；
- 失败不能伪装成 silence 或 success。

## 结构性债务

当前工程仍存在大 composition root、超大 Runtime/Core 文件、巨大单页前端以及新旧认知链并存等债务。重构必须服务于明确纵向切片，不以“目录更漂亮”为独立产品成果。
