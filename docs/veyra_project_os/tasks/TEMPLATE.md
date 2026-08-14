# LC-NNN — Task title

- Status: `DRAFT | ACTIVE | BLOCKED | COMPLETED`
- Owner/window:
- Base revision:
- Related roadmap stage:
- Related ADRs:

## User Outcome

完成后，用户第一次能够感受到什么？

## Why Now

它阻塞了哪个产品闭环或研究问题？

## Current Evidence

当前代码、运行态、自动化和 live 已经证明什么？哪些可能已过期？

## Required Reading

- `../README.md`
- 只列与本任务相关的概念/架构章节

## Research Question

本切片需要验证哪个仍未知的产品或架构假设？

## Scope

- 本轮必须完成的最小纵向链；
- 允许修改的职责/文件；
- 需要保留的现有行为。

## Non-goals

- 本轮明确不做什么；
- 不扩大到哪个 Roadmap 阶段；
- 不做哪些外部写入、迁移或重构。

## Proposed Boundary

这是 current approach，允许在不改变 User Outcome、不变量和 authority 的情况下调整。

## State and Migration

- authoritative state；
- schema/revision/CAS；
- replay/crash/retention；
- legacy compatibility；
- privacy projection。

## Authority Delta

| Dimension | Before | After | Evidence |
|---|---|---|---|
| Agent | | | |
| Tool/Grant | | | |
| Route/Risk | | | |
| External delivery | | | |
| Execution | | | |
| Data/source access | | | |

## Acceptance

### Automated

### Runtime/live

### User-visible

### Failure/negative

## Definition of Done

1. 代码实现了什么？
2. 用户第一次能够感受到什么？
3. 系统新增了什么长期能力？
4. 哪些假设仍未被证明？
5. authority 是否变化？
6. 文档、状态和 current task 是否同步？

若第 2 项无答案，标记 `INFRASTRUCTURE` 并说明解除的产品阻塞。

## Handoff

- final SHA/worktree；
- automated/live/user evidence；
- known degraded；
- rollback；
- next product loop。
