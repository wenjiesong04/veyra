# V1-001 — Living Context Alpha

- Status: `PRIVATE LOCAL ALPHA / IMPLEMENTED + AUTOMATED_VALIDATED / REAL_MODEL_VALIDATED PARTIAL / DEGRADED`
- Owner/window: root planning/review/docs; shared V1 implementation worktree
- Date: 2026-08-20
- Base revision: `cognitive-awakening @ 7439aeb` (V1 changes remain uncommitted)
- Related roadmap stages: `R1–R7 bounded alpha evidence`
- Related ADRs: ADR-001, ADR-002, ADR-003, ADR-004 (still Proposed)

## V0-001 disposition

`V0-001 — Local Product Preview` was explicitly expanded by the owner into this
V1 alpha. It is absorbed as the bounded product foundation, not reported as an
independent launch or as a completed Consumer V1. Any older Today-first wording
belongs to that historical preview and is superseded for the current information
architecture by the quiet First Meeting home, independent Today, Situations and
dedicated Chat routes described in `product_experience_v1.md`.

## User Outcome

在本地 alpha 中，用户可以把三类非硬编码的生活情境交给 Veyra 持续维护：

- 旅行、会议或活动安排；
- 求职、申请或学习计划；
- 搬家、个人项目或家庭计划。

同一条 Living Context 逻辑投影持续显示 Situation 的 Known、Unknown、
Assumptions、timeline、Information Need、evidence、material change 和
当前 reaction。用户能看到 Veyra 为什么现在建议、询问、读取、等待或沉默，
并可通过反馈影响 timing、cooldown 和 suppression；权限仍停留在本机、只读、
应用内、record-only 边界。

这是 `PRIVATE LOCAL ALPHA`，不是公开发布、外部交付产品或两周价值结论。

## Why Now

V0 的产品投影已经提供了可读的入口，但不能继续把一个预览首页或自动化绿灯
称作持续生活理解。当前切片把真实生活 Situation、显式 Information Need、
受治理 source、解释性 reaction 和反馈后效贯通到同一条 V1 alpha 链路，随后
用独立证据位置判断哪些结论仍需真实模型、干净运行态和用户样本。

## Current Evidence

### Implementation and automated evidence — final local gate

- `IMPLEMENTED + AUTOMATED_VALIDATED`：Python full gate `165/165`
  (`145` invariant + `1` cognitive + `19` product)；OpenClaw `32/32`；Route
  `810/810`。
- `IMPLEMENTED + AUTOMATED_VALIDATED`：frontend product-contract、status-tone、
  build、`check:bundle` 与 `build:desktop` 均 PASS；这些是构建/合同证据，不是
  浏览器或用户价值证据。
- `LOCAL PREVIEW / AUTOMATED_VALIDATED`：最终本地 arm64 Mach-O package PASS；Python
  `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`；sidecar smoke
  PASS、normalized cmp PASS、`codesign --verify --deep --strict` PASS；App 内
  `NSAppleEventsUsageDescription` 与 `automation.apple-events` entitlement 存在。
  sidecar SHA 为
  `471ef080f84639e8ca445fe20c91695095873a004854290008f6d1012304351`，Veyra.app
  tree SHA 为 `0257a1b8b004cbb95deee158164bacdc5481d6921193169b04e5a30a570ab590`。
  这仍是 ad-hoc local preview，不是 Developer ID/notarized/DMG/public release；
  实际 Calendar TCC 用户授权仍 pending。
- `IMPLEMENTED + AUTOMATED_VALIDATED`：通用自然语言路径覆盖三类 Situation、
  durable semantic fields、Information Need、read-only Calendar/Weather/Public
  Web source、ask/read/wait/silent/suggest 反应和 feedback timing/cooldown/
  suppression 边界；LivingReaction archive/retention 及 replay/tamper/scope
  checks 已纳入验证；Agent research 保持 disabled。

### Real-model and live evidence — deliberately separated

- `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED`：多次隔离运行的合并证据是三个通用
  场景 create `3/3` 曾成功、update `3/3` 曾成功、Calendar update `1/1` 曾成功，
  `ignore feedback` 曾被模型解析并持久记录；这些不是同一次 run，未在同一次 run
  完成全链。最终 run 为 create `3/3`、update `2/3`，因 timeline `source_quote`
  binding 波动而 DEGRADED。它证明通用路径曾经可用，但不能宣称单次全链 PASS、
  provider 稳定性、长期 usefulness 或 Jarvis 已证明。
- Calendar 的确定性完整 source→Need→understanding→reaction 链已验证，并有独立
  real provider read；Weather/Public Web 也有当前真实 read 证据。Calendar production
  consent/TCC contract 已实现，但实际 Calendar TCC 用户授权仍 pending。
- `BOUNDED_LIVE: PENDING`：clean runtime/restart identity、现场源读取、浏览器
  acceptance 和 served revision 尚未在本任务中闭合。
- `EXACT_SHA_CI / PUSH: PENDING`：当前工作树未提交，不能自证远端 CI 或 push；
  exact-SHA runtime、browser acceptance、owner acceptance 和 GitHub Actions 在提交
  推送前均保持 `PENDING`。
- `14_DAY_USEFULNESS: PENDING`、`USER_VALIDATED: PENDING`：没有两周真实样本、
  usefulness、漏报/误报、wrong timing 或 owner acceptance 结论。

## Scope

1. 维护三类真实生活 Situation 的同一套通用语义与 durable lifecycle；
2. 维护 Known/Unknown/Assumption/timeline/Need/evidence 的投影与可纠正性；
3. 维护 ask/read/wait/silent/suggest 的解释性 reaction；
4. 维护 Calendar、Weather、Public Web 的只读、exact-scope、consent、freshness
   和 receipt 边界；
5. 维护反馈对 timing、cooldown、suppression 的受限后效；
6. 维护 `#/` First Meeting、`#/today`、`#/situations`/detail、`#/chat/<id>`
   的产品信息架构；
7. 记录自动化、真实模型、source、feedback、runtime/live、浏览器和 CI 的
   独立 evidence position。

## Non-goals

- 不把 V0-001 伪称为独立上线，也不宣称 V1 已完成；
- 不把当前 `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` 升格为完整 validated，也不
  推断 `BOUNDED_LIVE`、`14_DAY_USEFULNESS` 或 `USER_VALIDATED`；
- 不启用 Agent research、外部 delivery、Agent/Tool/Grant/Route/Risk 或执行
  authority；
- 不把 Calendar、Weather、Public Web 的只读合同写成已经完成的实时个人来源接入；
- 不将 P3 identity/relationship 全面化、Phase 6 自扩展或通用 automation 作为 V1
  gate；
- 不新增 LivingContext aggregate truth，不迁移现有 authoritative state；
- 不修改代码、前端或 runtime state（本 task ownership 仅文档同步）。

## Authority Delta

| Dimension | Before | After | Evidence |
|---|---|---|---|
| Agent research | disabled | unchanged / disabled | automated boundary checks; live pending |
| Tool/Grant | no new product grant | unchanged | Route/authority regression |
| Route/Risk | existing contract | unchanged | Route `810/810` final local-gate snapshot |
| External delivery | disabled / record-only | unchanged | suggestion/reaction negative paths |
| Execution | none from alpha product path | unchanged | automated negative tests |
| Data/source access | local exact-scope state | read-only allowlisted source classes | source contract tests; live source pending |

## Acceptance

### Automated

Full gate `165/165` (`145` invariant + `1` cognitive + `19` product), OpenClaw
`32/32`, Route `810/810`, Web/Desktop build, product contract and bundle are green
in the final local gate. The arm64 package is a validated LOCAL PREVIEW with the
toolchain and hashes recorded above; it is not Developer ID/notarized/DMG/public
release evidence. This is still local worktree evidence until a committed revision,
exact-SHA push/CI and current live runtime are independently verified.

### Runtime/live

Clean restart/runtime identity, actual Calendar TCC user authorization,
browser/390px acceptance, served revision and exact-SHA CI are independent
positions and remain `PENDING`.

### User-visible

The intended alpha surface is route-separated First Meeting, Today, Situations,
Situation detail and dedicated Chat. It exposes evidence/unknown/reaction meaning,
not raw model payloads, private locators or execution claims. Actual owner
acceptance and two-week usefulness remain `PENDING`.

### Failure/negative

Scope mismatch, stale candidate/revision, unsupported source, source failure,
duplicate tick, feedback replay and authority escalation must fail closed or retain
an explicit degraded/unknown state. Green automation does not close live evidence.

## Definition of Done

1. Project OS and canonical links describe V1-001 as a private local alpha;
2. implementation and automated evidence are separated from real-model/live/user
   evidence;
3. the route architecture and current implementation boundaries agree;
4. all unproven evidence remains `PENDING` until root confirms it with fresh proof;
5. authority remains unchanged and Agent research/external delivery remain disabled;
6. the next handoff states exactly what root must rerun and what evidence is still
   missing.

## Handoff

- Worktree/base: `cognitive-awakening @ 7439aeb`; V1 implementation changes are
  uncommitted and owned by the other work window.
- Documentation changes in this task are limited to Project OS and the two
  canonical/addendum files named by the owner; no code, frontend or state was
  changed here.
- Remaining: resolve the timeline `source_quote` binding fluctuation and rerun one
  complete same-run real-model full-chain acceptance; then independent clean runtime, actual Calendar TCC user
  authorization, bounded live/browser, exact-SHA CI/push, and owner/two-week
  validation.
- Rollback: revert this documentation slice only; do not mutate user state or
  the V1 implementation worktree.
