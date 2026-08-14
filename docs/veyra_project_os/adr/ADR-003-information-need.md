# ADR-003 — 显式区分 Information Need 与 Observation

- Status: `Proposed`
- Date: 2026-08-14
- Owners: Veyra project owner
- Supersedes: none

## Problem

直接从 Situation 跳到 Probe、搜索或 Agent 调用，会让系统无目的收集信息，也无法解释为什么现在需要这条数据。只在 Prompt 中说“需要更多信息”又无法持久、调度和审计。

## Decision

在认知与信息获取之间显式引入 Information Need 语义：它描述缺什么、阻塞什么判断、为什么现在需要、何时失效以及允许哪类来源。

该决定规定语义边界，不强制同名类、Manager 或数据库。

## Alternatives

1. 模型直接生成 Tool/Probe 调用；
2. 每个 source 自己判断何时运行；
3. 仅在 Prompt/Brief 中记录未知；
4. 显式 Information Need，再由 server 决定如何满足。

## Why

它把“为什么需要信息”与“怎样获取信息”分开，使 ask、wait、source 和 Agent research 可以围绕同一未知竞争，同时保留 budget、scope 和用户控制。

## Consequences

- unresolved need 可以跨 tick 持续；
- 可以测量 resolution time、false need 和 wrong timing；
- 需要 dedupe、expiry、priority 和 lifecycle；
- 模型输出仍需 server validation；
- 可能增加状态复杂度，应优先以最小纵向切片验证。

## Non-goals

- 让模型生成任意查询或 Tool 参数；
- 将所有 unknown 自动变成 ObservationRequest；
- 无限制保持历史 needs；
- 因信息不足自动扩大权限。

## Revisit when

如果真实两周使用证明显式 need 没有优于轻量 ephemeral planning，或状态成本显著超过产品收益，应重新评估持久粒度。
