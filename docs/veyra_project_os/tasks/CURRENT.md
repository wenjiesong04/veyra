# V1-001 — Living Context Alpha

- Status: `PRIVATE LOCAL PREVIEW CANDIDATE / IMPLEMENTED + AUTOMATED_VALIDATED / REAL_MODEL_VALIDATED PARTIAL / DEGRADED`
- Owner/window: root planning/review/docs; shared V1 implementation worktree
- Date: 2026-08-22
- Base revision: `cognitive-awakening @ 34d44dfca0534f837b72b2ebe15949ed517ea699` (code checkpoint; this pass is docs-only)
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

这是 `PRIVATE LOCAL PREVIEW CANDIDATE`，不是 public stable release、公开发布、外部
交付产品或两周价值结论。

## Why Now

V0 的产品投影已经提供了可读的入口，但不能继续把一个预览首页或自动化绿灯
称作持续生活理解。当前切片把真实生活 Situation、显式 Information Need、
受治理 source、解释性 reaction 和反馈后效贯通到同一条 V1 alpha 链路，随后
用独立证据位置判断哪些结论仍需真实模型、干净运行态和用户样本。

## Current Evidence

### Implementation and automated evidence — final local gate

- `IMPLEMENTED + AUTOMATED_VALIDATED`：Python full gate `166/166`
  (`145` invariant + `1` cognitive + `20` product)；OpenClaw `32/32`；Route
  `810/810`。
- generic health/education/finance generalization smoke 通过；三个原始 live
  Situation 复用同一通用机制。
- `IMPLEMENTED + AUTOMATED_VALIDATED`：frontend product-contract、status-tone、
  build、`check:bundle` 与 `build:desktop` 均 PASS；这些是构建/合同证据，不是
  浏览器或用户价值证据。
- `LOCAL PREVIEW / AUTOMATED_VALIDATED`：最终 clean local arm64 Mach-O package PASS；
  Python `3.11.15`、PyInstaller `6.22.0`、Rust `1.96.1`、Tauri `2.11.4`；ad-hoc
  signed，sidecar smoke、normalized sidecar match、`codesign --verify --deep --strict`
  PASS；App 内 `NSAppleEventsUsageDescription` 与 `automation.apple-events` entitlement
  存在。sidecar SHA 为
  `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`，Veyra.app SHA
  为 `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab`。这仍不是
  Developer ID、notarized、DMG 或 public release；Calendar real TCC 与 Weather/Public
  Web user consent 仍 pending。
- frontend Home/Today/Situations/Chat/Settings 与 `390x844` browser acceptance 通过；
  Context gating、404 UX、source labels、question dedupe 已核验。
- `IMPLEMENTED + AUTOMATED_VALIDATED`：通用自然语言路径覆盖三类 Situation、
  durable semantic fields、Information Need、read-only Calendar/Weather/Public
  Web source、ask/read/wait/silent/suggest 反应和 feedback timing/cooldown/
  suppression 边界；LivingReaction archive/retention 及 replay/tamper/scope
  checks 已纳入验证；Agent research 保持 disabled。

### Real-model and live evidence — deliberately separated

- `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED`：isolated real-model asked-answer
  series 为 `3/3`；long-session move update 已解析 mover Information Need，同时
  保留 network/date Unknowns。更广泛的 natural-language model reliability 仍是
  `PARTIAL / DEGRADED`，不证明稳定 provider、完整通用全链、长期 usefulness 或
  Jarvis-like relationship。
- Calendar 的确定性完整 source→Need→understanding→reaction 链已验证，并有独立
  read-path evidence；Weather/Public Web 的只读合同与 read path 已覆盖，但 Calendar
  real TCC、Weather/Public Web user consent 仍 pending。
- `BOUNDED_LIVE: VALIDATED`：重启后的 startup `runtime_build` 报告 `34d44df`、
  `dirty=false`、`loaded_code_attested=false`，current served UI/browser validation 通过；Calendar/Weather/Public
  Web user consent 仍独立 pending。
- `EXACT_SHA_CI: VALIDATED`：GitHub Actions run `32564052536` 对 `34d44df` 成功：
  <https://github.com/wenjiesong04/veyra/actions/runs/32564052536>。这不等于 public
  delivery/release；`77dbaf5` 成功仍保留为历史 evidence。
- `SUSTAINED_USEFULNESS: PENDING`、`USER_VALIDATED: PENDING`：没有 sustained 7–14 day
  usefulness/quiet-rate 样本、漏报/误报、wrong timing 或 owner acceptance 结论。

## Scope

1. 维护三类真实生活 Situation 的同一套通用语义与 durable lifecycle；
2. 维护 Known/Unknown/Assumption/timeline/Need/evidence 的投影与可纠正性；
3. 维护 ask/read/wait/silent/suggest 的解释性 reaction；
4. 维护 Calendar、Weather、Public Web 的只读、exact-scope、consent、freshness
   和 receipt 边界；
5. 维护反馈对 timing、cooldown、suppression 的受限后效；
6. 维护 `#/` First Meeting、`#/today`、`#/situations`/detail、`#/chat/<id>`、
   `#/settings` 的产品信息架构；
7. 记录自动化、真实模型、source、feedback、runtime/live、浏览器和 CI 的
   独立 evidence position。

## Non-goals

- 不把 V0-001 伪称为独立上线，也不宣称 V1 已成为 public stable release 或所有后续阶段已完成；
- 不把当前 `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` 升格为完整 validated，也不
  把已验证的 bounded runtime/browser 证据外推为 source consent、
  `SUSTAINED_USEFULNESS` 或 `USER_VALIDATED`；
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

Full gate `166/166` (`145` invariant + `1` cognitive + `20` product), OpenClaw
`32/32`, Route `810/810`, Web/Desktop build, product contract and bundle are green
in the final local gate. Generic health/education/finance generalization smoke and
the three original live Situations on one mechanism are also recorded. The arm64
package is a validated LOCAL PREVIEW with the toolchain and hashes recorded above;
it is not Developer ID/notarized/DMG/public release evidence. Exact-SHA Actions
run `32564052536` and exact-revision runtime/browser evidence are validated for the
bounded core product.

### Runtime/live

Exact-revision clean restart/runtime identity, current served UI and desktop/
`390x844` browser acceptance are validated, including Context gating, 404 UX,
source labels and question dedupe. Actual Calendar TCC, Weather/Public Web user
consent, sustained 7–14 day usefulness/quiet-rate and owner sign-off remain
independent `PENDING` positions.

### User-visible

The intended preview surface is route-separated First Meeting, Today, Situations,
Situation detail, dedicated Chat and Settings. It exposes evidence/unknown/reaction
meaning, not raw model payloads, private locators or execution claims. Actual owner
acceptance and sustained 7–14 day usefulness/quiet-rate remain `PENDING`.

### Failure/negative

Scope mismatch, stale candidate/revision, unsupported source, source failure,
duplicate tick, feedback replay and authority escalation must fail closed or retain
an explicit degraded/unknown state. Green automation does not close live evidence.

## Definition of Done

1. Project OS and canonical links describe V1-001 as a private/local preview candidate;
2. implementation and automated evidence are separated from real-model/live/user
   evidence;
3. the route architecture and current implementation boundaries agree;
4. all unproven evidence remains `PENDING` until root confirms it with fresh proof;
5. authority remains unchanged and Agent research/external delivery remain disabled;
6. the next handoff states exactly what root must rerun and what evidence is still
   missing;
7. no claim is made that all P stages are complete; P1/P2, sustained usefulness,
   source consent and public release remain independently bounded.

## Handoff

- Code checkpoint: `cognitive-awakening @ 34d44dfca0534f837b72b2ebe15949ed517ea699`;
  this handoff changes docs only and does not claim current docs-tree cleanliness.
- Documentation changes in this task are limited to Project OS and the two
  canonical/addendum files named by the owner; no code, frontend or state was
  changed here.
- Remaining: actual Calendar TCC and Weather/Public Web user consent, sustained
  7–14 day usefulness/quiet-rate and owner validation, plus notarization/DMG/public
  release evidence. Broader natural-language model
  reliability remains `PARTIAL / DEGRADED`; the `3/3` asked-answer series is bounded
  evidence, not completion of all P stages.
- Rollback: revert this documentation slice only; do not mutate user state or
  the V1 implementation worktree.
