# ADR-001 — Living Context 是逻辑投影

- Status: `Proposed`
- Date: 2026-08-14
- Owners: Veyra project owner
- Supersedes: none

## Problem

“World State”过于系统化，无法完整表达用户、Goal、Relationship、Time、Unknown 和 Attention 的综合体验。但如果把 Living Context 直接实现成新对象或数据库，会复制 truth、制造 God Object 和迁移负担。

## Decision

Living Context 被定义为 exact-scope、time-aware、evidence-bound 的 **logical projection**。

它从现有权威状态按需组合，不拥有第二套事实，不要求同名类或持久文件。

## Alternatives

1. 继续只使用 World State；
2. 建立统一 LivingContext 数据库；
3. 每次仅靠 Prompt 汇总；
4. 使用 logical projection。

## Why

Logical projection 同时支持产品语言和工程边界：用户可以看到综合理解，底层 Goal、Commitment、Belief、Situation 和 Authority 仍保持独立 truth 与生命周期。

## Consequences

正面：

- 产品表达更贴近用户；
- 避免第二套 truth；
- projection 可按页面和任务演进；
- 可以显示来源、新鲜度和未知。

代价：

- 需要明确 projection 版本和 scope；
- 聚合读取需要性能和一致性设计；
- 不能依赖单一对象完成跨状态原子写。

## Non-goals

- 创建 `LivingContextManager`；
- 合并所有 state schema；
- 用 projection 反向绕过原 writer；
- 把 projection 当作事实权威。

## Revisit when

只有当现有分散 truth 无法满足一致性、性能或迁移需求，并有真实产品证据支持新持久模型时重新评估。
