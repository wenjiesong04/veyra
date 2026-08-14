# V0-001 — Local Product Preview

- Status: `ACTIVE`
- Owner/window: root planning/review/docs; `luna_worker` implementation
- Date: 2026-08-14
- Base revision: `da53ef8c5501bcd1704e808ebc1c461f6fdcc2b0` plus the owner's uncommitted product-frontend work
- Related roadmap stages: `R0 + bounded R5 preview`
- Related ADRs: ADR-001, ADR-002 (still Proposed)

## User Outcome

用户打开 Veyra 后，不再先面对聊天壳或运维仪表盘，而能在十秒内看见：

- Veyra 当前持续关注什么；
- 最近形成了什么 Situation；
- 是否存在有依据的 suggestion、question 或 waiting state；
- 数据来自哪里、是否新鲜、Veyra 被允许做什么；
- 如何继续对话、纠正、暂停或进入 Advanced 查看技术证据。

这是一个 local-first `0.1 Product Preview`，不是 Consumer V1 或公开云服务。

## Why Now

当前认知基础设施已有 production-shaped Workspace Observer canary，但新产品前端仍以 Chat 为首页，并从 `/state`、全局 Review 和占位对象拼接 Matters/Status。真实 owner/session、record-only proposal 与普通用户页面尚未形成一致合同。

继续完成全部 P 阶段会推迟第一次可用产品反馈；只发布换皮前端又会掩盖后端事实。本切片先完成诚实、受限的产品读面和本机交互入口。

## Current Evidence

- `da53ef8` 的 Python gate、Route 810 matrix、Web/Desktop build 和 exact-SHA CI 曾通过；未提交前端不继承该证据；
- 当前真实 workspace Goal、Situation、Attention 与 record-only suggestion 已存在一条 bounded canary；
- Generic Cognitive Loop 的 `0 candidate / overconservative` 仍是已知产品缺口；
- 新前端可以构建，Advanced 保留旧控制台，但默认 scope、Matters 和 Status 存在确定的前后端错位；
- conversation history 仍只有浏览器本地记录，没有 durable server conversation store。

## Required Reading

- [`../README.md`](../README.md)
- [`../living_context.md`](../living_context.md)
- [`../product_experience_v1.md`](../product_experience_v1.md)
- [`../architecture/overview.md`](../architecture/overview.md)
- [`../architecture/authority.md`](../architecture/authority.md)
- [`../definition_of_done.md`](../definition_of_done.md)

## Research Question

在不新增外部写入或自治权限的前提下，Situation-first 的 Today/Matters 投影是否能让用户正确说明“Veyra 正在理解什么、依据是什么、现在能做什么”，并比 Chat-first/Console-first 更容易开始持续使用？

## Scope

1. 建立 versioned、纯读、exact-scope 的 product-facing API；
2. 由服务端选择或诚实拒绝 primary local product context，前端不再硬编码伪 scope；
3. 将 Today 设为默认产品页，并对齐 Goal/Commitment/Situation/Attention/Suggestion/Question/Waiting；
4. Matters 不再读取 operator-wide Review、猜测 `/state` 数组或展示不可用假卡片；
5. Status 只显示产品健康、新鲜度、来源、证据等级和 authority；完整细节进入 Advanced；
6. Settings 展示 Sources & Permissions、本机边界和 stop/setup 入口；
7. Conversation 继续使用真实 lifecycle SSE，不伪造 token stream，并最小化浏览器持久化数据；
8. 保留当前视觉语言、Setup Wizard 与旧 Advanced Console；
9. 完成本机浏览器、390px、Web/Desktop build、full gate 与 clean-runtime 验收。

## Non-goals

- 不完成全部 P1–P8 或把一个 UI 切片称为 Living Context V1；
- 不在本轮实现 Calendar、Email、Agent research、通用 Observation Broker 或 durable conversation store；
- 不启用 `ask`、外部主动推送、Agent/Tool execution 或受治理学习 aftereffect；
- 不修改 Route/Risk、Review、Grant、Phase 6 或 provider/model 配置；
- 不承诺签名、公证、App Store、Windows/Linux 包或公网托管；
- 不删除 Developer Console，不批量迁移旧文档。

## Proposed Boundary

- 新 product read-model/service 聚合现有 authoritative runtime，不复制 truth；
- 新 product router 只公开普通用户需要的 projection；
- record-only suggestion 可以作为明确标注的 preview 显示，但 `delivery=none`；
- primary context 优先绑定唯一 active Workspace Goal；无匹配、多个候选或 session mismatch 时诚实返回 setup/ambiguous 状态；
- loopback/Tauri 是 0.1 支持边界，非-loopback 继续受现有 local control token 约束。

## State and Migration

- 本切片优先不新增 durable truth；product pages 是现有 Goal、Commitment、Situation、Attention、Suggestion 和 runtime 状态的逻辑投影；
- GET 必须纯读，跨 owner/session fail closed；
- 浏览器历史只保留白名单展示字段，不持久完整 artifacts；
- 现有 localStorage 历史在读取时清洗；
- 不迁移或删除本地 state。

## Authority Delta

| Dimension | Before | After | Evidence |
|---|---|---|---|
| Agent | governed, no new product dispatch | unchanged | Route/authority regression |
| Tool/Grant | no new grant | unchanged | product API is read-only |
| Route/Risk | 9 Routes, existing risk contract | unchanged | 810 matrix |
| External delivery | disabled for preview | unchanged | record-only proposal projection |
| Execution | none from product read model | unchanged | negative tests |
| Data/source access | existing local exact-scope state | curated product projection only | scope/privacy tests |

## Acceptance

### Automated

- product API exact-scope, ambiguity, fallback and privacy tests；
- record-only preview remains non-delivering with all authority false；
- frontend contract, bundle freshness and status evidence-level tests；
- existing suggestion/structured-observation/Route regressions；
- full Python gate, plugin tests, compileall, Web and Desktop build。

### Runtime/live

- clean exact-SHA restart；
- current main test identity resolves to the existing exact workspace Goal scope；
- Today shows the bounded real Goal → Situation → Attention → record-only suggestion chain；
- consecutive product GETs do not mutate business state；
- one real SSE conversation completes without changing external-delivery authority。

### User-visible

- desktop and 390px views are readable；
- first screen answers what Veyra is following, what changed, what is unknown/waiting and what it can do；
- no raw path, token, digest wall, fake Goal/Case/Task or false validation claim；
- Advanced remains reachable but is not the default experience。

### Failure/negative

- zero/multiple/mismatched context is explicit, not cross-scope fallback；
- unavailable sections keep their own error/freshness state；
- no suggestion/question is shown when no current canonical record exists；
- production validation pending cannot be rendered as validated。

## Definition of Done

1. 代码提供一个稳定、纯读、exact-scope 的产品 projection；
2. 用户第一次能够在 Today/Matters 看见 Veyra 的真实持续理解，而非内部 JSON；
3. 新前端与同一后端 scope、evidence 和 authority 合同一致；
4. 未证明项继续标 `PARTIAL`/`PENDING`；
5. authority 无扩张；
6. Project OS、status/current、canonical 入口、验证证据和下一任务同步。

## Handoff

完成时记录：final SHA、工作树、自动化、runtime/browser/mobile证据、known degraded、rollback 和下一条真实非代码 `LC-001`。
