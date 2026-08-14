# Architecture Decision Records

ADR 记录“为什么做出一个有长期后果的决定”，不是重复 Architecture 正文。

## 状态

- `Proposed`
- `Accepted`
- `Rejected`
- `Superseded by ADR-XXX`
- `Deprecated`

## 何时写 ADR

满足至少一项：

- 存在两个以上合理替代方案；
- 决定影响多个模块或长期数据；
- 未来推翻需要迁移；
- 决定定义重要产品/权限边界；
- 团队以后很可能问“为什么这样做”。

普通 bugfix、命名、局部重构和 Current Task 施工步骤不写 ADR。

## 索引

| ADR | 状态 | 决定 |
|---|---|---|
| [ADR-001](ADR-001-living-context-logical-projection.md) | Proposed | Living Context 是 logical projection |
| [ADR-002](ADR-002-active-concern-logical-projection.md) | Proposed | Active Concern 统一关注视角，不统一实体 |
| [ADR-003](ADR-003-information-need.md) | Proposed | 显式区分信息需求与信息获取 |
| [ADR-004](ADR-004-governed-observation-request.md) | Proposed | ObservationRequest 由 server 治理并映射 source |

## 模板

```markdown
# ADR-XXX — Title

- Status:
- Date:
- Owners:
- Supersedes:

## Problem

## Decision

## Alternatives

## Why

## Consequences

## Non-goals

## Revisit when
```
