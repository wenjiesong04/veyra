# V0-001 — Local Product Preview

- Status: `LOCAL_VALIDATED / OWNER_ACCEPTANCE_PENDING`
- Owner/window: root planning/review/docs; `luna_worker` implementation
- Date: 2026-08-14
- Final evidence revision: `e5fcf80ad3e1e6e54f3602603bdf941e5aa8fd37`
- Feature slice: `80ff937` (`feat(product): ship local Veyra preview`)
- Related roadmap stages: `R0 + bounded R5 preview`
- Related ADRs: ADR-001, ADR-002 (still Proposed)

## User Outcome

用户打开 Veyra 后，不再先面对聊天壳或运维仪表盘，而能在十秒内看见：

- Veyra 当前持续关注什么；
- 最近形成了什么 Situation；
- 是否存在有依据的 suggestion、question 或 waiting state；
- 数据来自哪里、是否新鲜、Veyra 被允许做什么；
- 如何继续对话、纠正、暂停或进入 Advanced 查看技术证据。

这是一个 local-first `0.1 Product Preview`，不是 Consumer V1、公开云服务或外部交付产品。

## Why Now

Workspace Observer 已提供 production-shaped trusted canary，但此前默认页、Matters、Status 与后端 exact scope 不一致。这个切片先把真实 Goal/Situation/Attention/record-only suggestion 投影成可读的 Today/Matters/Status，并保留 Advanced 与现有 authority 边界，尽快获得真实用户反馈。

## Required Reading

- [`../README.md`](../README.md)
- [`../living_context.md`](../living_context.md)
- [`../product_experience_v1.md`](../product_experience_v1.md)
- [`../architecture/overview.md`](../architecture/overview.md)
- [`../architecture/authority.md`](../architecture/authority.md)
- [`../definition_of_done.md`](../definition_of_done.md)

## Current Evidence

- `e5fcf80` runtime v2 clean evidence：Python 3.11.15、`dirty_flag=false`、exact build identity；业务状态 GET 前后 byte-pure。
- Python gate `148/148`：`145` invariant + `1` cognitive capability + `2` product capability；OpenClaw plugin `32/32`；9-route matrix `810/810`。
- Product smoke 覆盖 exact owner/session、ambiguous/mismatch/malformed fail-closed、stable Matters section schema、record-only preview、status evidence and GET purity；SSE smoke 覆盖真实 lifecycle、duplicate/rejected/failed typed result 和无 token/CoT。
- Web production/Desktop frontend build、browser acceptance、390×844 readability、shared history sanitizer、`/product` proxy/contract 均通过。
- Clean `build_desktop.sh package` on `e5fcf80` exited `0`: local `.app` is ad-hoc Apple Silicon arm64; sidecar is Mach-O arm64, Python `3.11.15`, PyInstaller `6.22.0`; sidecar smoke, strict codesign and normalized payload comparison passed. App tree SHA-256: `c62ac2a0c69865278ecc4096ac69105442e985c0aec0ee5b3c5f3ae0666abeca`; sidecar SHA-256: `a996f308d0c18443238c646c143f1966b66d8944b1ed707dc46be33264c95501`.
- Final branch push and exact-SHA Actions must be verified externally at handoff; this SHA-producing document does not self-attest them. Signing is `ad_hoc_not_notarized_local_preview`, not Developer ID, notarized, DMG or store release.

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
9. 通过本机浏览器、390px、Web/Desktop build、product capability smoke、runtime purity 和 Apple Silicon local package 验收。

## Non-goals

- 不完成全部 P1–P8 或把一个 UI 切片称为 Living Context V1；
- 不在本轮实现 Calendar、Email、Agent research、通用 Observation Broker 或 durable conversation store；
- 不启用 `ask`、外部主动推送、Agent/Tool execution 或受治理学习 aftereffect；
- 不修改 Route/Risk、Review、Grant、Phase 6 或 provider/model 配置；
- 不承诺 Developer ID、notarization、DMG、App Store、Windows/Linux 包或公网托管；
- 不删除 Developer Console，不批量迁移旧文档。

## Authority Delta

| Dimension | Before | After | Evidence |
|---|---|---|---|
| Agent | governed, no new product dispatch | unchanged | Route/authority regression |
| Tool/Grant | no new grant | unchanged | product API is read-only |
| Route/Risk | 9 Routes, existing risk contract | unchanged | 810 matrix |
| External delivery | disabled for preview | unchanged | record-only proposal projection |
| Execution | none from product read model | unchanged | negative tests |
| Data/source access | existing local exact-scope state | curated product projection only | scope/privacy tests |

## State and Migration

- Product pages remain logical projections of existing Goal, Commitment,
  Situation, Attention, Suggestion and runtime state; no new durable truth or
  state migration is introduced.
- GETs are pure reads and cross-owner/session access fails closed. Browser
  history persists only the allow-listed display fields and is sanitized on
  legacy reads.

## Acceptance

### Automated — validated

- `148/148` Python gate, with `145/145` invariant, `1/1` cognitive capability and `2/2` product capability;
- OpenClaw plugin `32/32`, Route `810/810`, structured observation and suggestion/interaction regressions;
- product API exact scope/privacy/ambiguity, record-only no-delivery/all-authority-false, malformed/degraded source, status evidence, and GET byte-purity tests;
- SSE lifecycle smoke and frontend product contract, bundle, status-tone, Web/Desktop build checks.

### Runtime/live — bounded evidence

- clean runtime v2 identity at `e5fcf80`, loopback/Tauri boundary, browser acceptance and 390×844 review;
- local arm64 app package, sidecar Mach-O/Python 3.11.15/PyInstaller 6.22.0, sidecar smoke, strict codesign and normalized sidecar comparison;
- no claim of Feishu external delivery, production Economy validation or long-term usefulness.

### User-visible — validated within the preview boundary

- Today-first home answers what Veyra follows, what changed, what remains
  unknown/waiting and what it can do; Matters/Status/Settings remain readable;
  Advanced is reachable but not the default.
- No raw path, token, digest wall, fake Goal/Case/Task or false production
  validation claim is exposed in the ordinary product surface.

### Failure/negative — validated

- zero/multiple/mismatched context is explicit, not cross-scope fallback;
- unavailable or malformed sections retain fail-closed/degraded source status;
- no current canonical record means no suggestion/question is shown;
- production-pending evidence cannot render as validated or live.

### Remaining acceptance evidence

- final branch push and exact-SHA GitHub Actions must pass and be linked in the handoff;
- the owner has not yet accepted the packaged local preview as the first usable release;
- P1/P2 remain `PARTIAL`; user usefulness is not validated.

## Definition of Done

1. 代码提供稳定、纯读、exact-scope 的产品 projection；
2. 用户第一次能够在 Today/Matters 看见 Veyra 的真实持续理解，而非内部 JSON；
3. 新前端与同一后端 scope、evidence 和 authority 合同一致；
4. 未证明项继续标 `PARTIAL`/`PENDING`；
5. authority 无扩张；
6. Project OS、status/current、canonical 入口、验证证据和下一任务同步；
7. clean local package 已验证；最终 branch push/exact-SHA Actions 在 handoff 外部核验，并且只有 owner 明确接受本地预览后，V0-001 才可归档，不得提前 `COMPLETED`。

## Handoff

- Final code/runtime evidence: `e5fcf80`; clean local app package evidence is recorded above.
- Known degraded: P1/P2 partial, user usefulness/timing unvalidated, `ask` dormant, no external delivery, no durable conversation store, production Economy metadata incomplete.
- Rollback: retain the prior console route and remove the product router/UI changes as one release slice; do not mutate user state.
- Next planned task: `LC-001 — Real non-code Situation`; it remains planned until V0-001 release evidence is closed and a new `CURRENT.md` is created.
