# Architecture Index

Architecture 描述 **current approach**：当前 V1 alpha 怎样组织已有能力以实现产品目标。它可以演进，不是 North Star，也不是永久类图。实现与 automated evidence 已有 bounded slice；real-model、live、browser、CI 和 user-value evidence 仍按状态文档独立记录。

## 阅读路径

```text
Runtime wake/event substrate
          ↓
Observation and evidence acquisition
          ↓
Cognition and logical projections
          ↓
Reaction / Delegation
          ↓
Authority and verification
          ↓
Feedback and calibration
```

## 子文档

| 文档 | 主题 | 何时读取 |
|---|---|---|
| [architecture/overview.md](architecture/overview.md) | 全局边界、现状映射、可替换性 | 架构审查和跨模块任务 |
| [architecture/runtime.md](architecture/runtime.md) | 前台、事件、deadline、heartbeat、持久循环 | 生命周期与运行任务 |
| [architecture/cognition.md](architecture/cognition.md) | Concern、Situation、Information Need、Attention、Reaction | 认知与产品闭环任务 |
| [architecture/observation.md](architecture/observation.md) | ObservationRequest、source、evidence | 传感器和外部来源任务 |
| [architecture/delegation.md](architecture/delegation.md) | Agent/Skill/Probe 委托边界 | Agent 与研究/执行任务 |
| [architecture/authority.md](architecture/authority.md) | scope、permission、risk、verification | 任何可能产生副作用的任务 |
| [architecture/scheduler.md](architecture/scheduler.md) | wake、优先级、预算、冷却 | 后台循环和主动时机任务 |
| [architecture/feedback.md](architecture/feedback.md) | 反馈、校准、受治理后效性 | learning 与产品评估任务 |

## 每份 Architecture 文档的写法

重要方案应明确：

```text
Status: proposed | current approach | implemented | deprecated
Why chosen:
Alternatives considered:
Replaceable parts:
Non-negotiable invariants:
Evidence:
Open questions:
```

禁止把文件名、类名或今天的模块边界写成产品定义。若重要决定发生变化，新增 ADR，而不是静默改写原因。
