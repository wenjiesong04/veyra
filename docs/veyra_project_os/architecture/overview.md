# Architecture Overview

## Status

`CURRENT DIRECTION / PARTIALLY IMPLEMENTED`

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
