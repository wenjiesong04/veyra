# ADR-004 — ObservationRequest 由 Server 治理

- Status: `Proposed`
- Date: 2026-08-14
- Owners: Veyra project owner
- Supersedes: none

## Problem

模型、Skill 或 Agent 如果可以根据 Information Need 自由构造 path、URL、query、command 或 Tool 参数，就可能越过数据用途、权限和副作用边界。全部硬编码场景又无法扩展到真实生活。

## Decision

Information Need 经过 server-owned admission 后，映射成受治理的 ObservationRequest，再选择 allowlisted Source Capability。

模型表达 evidence need；server 拥有 scope、consent、source mapping、budget、TTL、request identity 和 receipt admission。

具体 Broker、Registry、Pydantic 类和模块组织属于可替换 current approach。

## Alternatives

1. 模型直接调用 Tool/Agent；
2. 每个 Skill 自己决定权限；
3. 为每个场景硬编码完整流程；
4. Server-governed request + source capability。

## Why

该边界允许增加 Calendar、Message、Web、Probe 和 Agent research，而不让新来源获得 Veyra 的长期状态权威或执行权。

## Consequences

- 每个 source 需要注册、schema、consent 和 receipt；
- 未知 source 必须 unavailable，而不是自由 fallback；
- request/receipt 需要 replay、timeout 和 retention；
- 可以独立替换 source adapter；
- 早期实现应避免建立过度通用 framework。

## Non-goals

- 通用 Tool execution broker；
- 任意外部写入；
- 把 Skill 视为安全边界；
- 让 Observation 自动成为 verified fact；
- 自动接入全部个人数据来源。

## Revisit when

当多个真实 source 证明当前 mapping 过于僵化，或 auth-derived principal、流式 source、设备端隐私需要新信任模型时重新评估。
