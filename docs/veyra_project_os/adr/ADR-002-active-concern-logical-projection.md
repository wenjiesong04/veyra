# ADR-002 — Active Concern 统一关注视角，不统一实体

- Status: `Proposed`
- Date: 2026-08-14
- Owners: Veyra project owner
- Supersedes: none

## Problem

如果认知只从 Goal 开始，会漏掉先出现的风险、机会、关系变化、承诺和意外事件。若为它们建立统一父类，又会抹掉不同来源和生命周期。

## Decision

使用 `Active Concern (logical projection)` 表示当前值得持续关注的事情。

Goal、Commitment、Risk、Opportunity、Relationship concern 和重要变化可以进入该视角，但保留各自 authoritative schema。Concern 不自动成为 Goal。

## Alternatives

1. 所有关注必须先创建 Goal；
2. 将所有类型继承统一 Concern 实体；
3. 每个模块独立排序；
4. 使用跨类型 logical projection。

## Why

该决定允许现实先于用户显式规划，同时避免 Veyra 擅自替用户定义目标。

## Consequences

- Attention 可以跨 Goal、Risk 和 Commitment 比较；
- UI 可以统一展示“正在关心的事情”；
- 需要稳定 projection identity 和来源引用；
- 跨类型排序仍是研究问题；
- 任何写回必须回到原 authoritative writer。

## Non-goals

- 新建 Concern 总表；
- 自动把检测到的变化转为长期 Goal；
- 统一各类 lifecycle；
- 让 projection 获得 authority。

## Revisit when

当真实使用证明跨类型 projection 无法提供稳定身份或排序，且统一实体的收益大于迁移风险时重新评估。
