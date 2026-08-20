# Current Status

> Evidence snapshot date: 2026-08-20 (Asia/Shanghai)
>
> 本页是 Project OS 的 current Living truth。当前 V1 工作树仍是未提交的
> local alpha；自动化已收口，但没有把真实模型、clean runtime、live、浏览器
> 或远端 CI 证据互相继承。未闭合的证据位置保持 `PENDING`，不稳定的真实模型
> 证据标为 `PARTIAL / DEGRADED`。

## Release class and evidence ladder

| Dimension | Current label | Boundary |
|---|---|---|
| Release class | `PRIVATE LOCAL ALPHA` | 本机 alpha；不是公开发布或外部交付产品 |
| Implementation | `IMPLEMENTED` | V1 semantic/reaction/source/product paths exist in the shared worktree |
| Automated | `AUTOMATED_VALIDATED` | final local gate `165/165` (`145` invariant + `1` cognitive + `19` product), OpenClaw `32/32`, Route `810/810`, Web/Desktop build/product contract/bundle green |
| Real model | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | multiple isolated Moonshot runs together show three generic scenarios with create `3/3` once successful, update `3/3` once successful, Calendar update `1/1` once successful, and model-parsed/persisted `ignore feedback`; no single run completed the full chain. Final run: create `3/3`, update `2/3`, degraded by timeline `source_quote` binding fluctuation |
| Bounded live | `BOUNDED_LIVE: PENDING` | clean runtime/restart identity, current source/live and browser acceptance not closed |
| Two-week usefulness | `14_DAY_USEFULNESS: PENDING` | no 14-day owner sample or usefulness thresholds |
| User validation | `USER_VALIDATED: PENDING` | owner acceptance and correction/timing evidence not closed |
| Authority | `RECORD_ONLY / NO EXTERNAL DELIVERY` | Agent research, Tool/Grant expansion and external delivery remain disabled |

## Current revision and worktree

- Branch: `cognitive-awakening`.
- Base HEAD observed for this documentation pass: `7439aeb`.
- V1 implementation changes are uncommitted in the shared worktree; this status
  page does not claim a final SHA, clean tree, push or exact-SHA Actions result.
- The final local gate is green, but the worktree remains uncommitted. This page
  does not claim a final SHA, clean tree, push or exact-SHA Actions result.

## V1-001 — Living Context Alpha

Status: `PRIVATE LOCAL ALPHA / IMPLEMENTED + AUTOMATED_VALIDATED / REAL_MODEL_VALIDATED PARTIAL / DEGRADED`

V1 maintains one Living Context logical projection for three non-hardcoded life
Situation families: travel/meeting/event arrangements, job-search/application/
study plans, and moving/personal-project/family plans. The durable semantic view
keeps Known, Unknown, Assumptions, timeline, evidence, Information Need and
material change tied to exact owner/session/Situation scope.

The reaction surface is bounded to `ask`, `read`, `wait`, `silent` and `suggest`
with an explanation of what happened, why it matters, why now and the next step.
Feedback can affect timing, cooldown and suppression within the governed local
ledger, and LivingReaction history has a bounded archive/retention path with
replay and scope/tamper checks. Calendar, Weather and Public Web are read-only
source classes with server-owned scope/consent/freshness/receipt boundaries; a
deterministic Calendar chain and separate real provider read are evidenced, as are
current real reads for Weather and Public Web. Agent research and external
delivery are disabled.

### Product information architecture

The current product route contract is:

- `#/` — independent quiet First Meeting home;
- `#/today` — independent Today view, not the default home;
- `#/situations` and Situation detail — durable concerns, evidence, unknowns and
  reactions;
- `#/chat/<id>` — dedicated Chat for input, answers and corrections.

Advanced/Developer Console remains a separate technical surface. A successful
build does not prove these routes were accepted in a live browser.

### Evidence ledger

| Evidence position | Current state | What it supports | What it does not support |
|---|---|---|---|
| Python full gate | `AUTOMATED_VALIDATED` | final `165/165` = `145` invariant + `1` cognitive + `19` product | user value |
| OpenClaw / Route | `AUTOMATED_VALIDATED` | OpenClaw `32/32`; Route `810/810`; governance/non-regression boundaries | external connectivity or delivery |
| Web/Desktop build, product contract, bundle | `AUTOMATED_VALIDATED` | frontend product-contract, status-tone, build, `check:bundle` and `build:desktop` all PASS | browser acceptance, served revision, mobile readability |
| Local desktop package | `LOCAL PREVIEW / AUTOMATED_VALIDATED` | final local arm64 Mach-O package PASS; Python `3.11.15`, PyInstaller `6.22.0`, Rust `1.96.1`, Tauri `2.11.4`; sidecar smoke PASS, normalized cmp PASS, `codesign --verify --deep --strict` PASS; App `NSAppleEventsUsageDescription` and `automation.apple-events` entitlement present; sidecar SHA `471ef080f84639e8ca445fe20c91695095873a004854290008f6d1012304351`, Veyra.app tree SHA `0257a1b8b004cbb95deee158164bacdc5481d6921193169b04e5a30a570ab590` | actual Calendar TCC user authorization; ad-hoc local preview, not Developer ID/notarized/DMG/public release |
| Moonshot natural input | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | multiple isolated runs together show three generic scenarios with create `3/3` once successful, update `3/3` once successful, Calendar update `1/1` once successful, and model-parsed/persisted `ignore feedback`; no single run completed the full chain. Final run: create `3/3`, update `2/3`, degraded by timeline `source_quote` binding fluctuation | no single-run full-chain PASS; provider stability, long-term usefulness and Jarvis remain unproven |
| Calendar / Weather / Public Web | `IMPLEMENTED + AUTOMATED_VALIDATED + REAL_SOURCE_READ_OBSERVED` | read-only source contracts; deterministic Calendar full chain; separate real Calendar/Weather/Public Web reads | actual packaged-app Calendar TCC authorization, restart and current served revision |
| LivingReaction retention | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded archive/retention, replay and tamper/scope failure paths | long-term owner usage and operational soak |
| Feedback | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded timing/cooldown/suppression ledger behavior | long-term calibration or user usefulness |
| Clean runtime/restart | `BOUNDED_LIVE: PENDING` | — | runtime identity, restart and current served revision |
| Browser / 390×844 | `BOUNDED_LIVE: PENDING` | — | route, layout and data-contract acceptance |
| Push / exact-SHA CI | `PENDING` | — | remote build or delivery |
| 14-day usefulness / owner validation | `PENDING` | — | usefulness, false silence, wrong timing, trust or launch readiness |

## V0-001 disposition

`V0-001 — Local Product Preview` is explicitly absorbed into V1-001 by the
owner-expanded scope. Its bounded product projection and historical local package
remain context, but it is not recorded as an independent V1 launch, Consumer V1,
or user-validated release. Older V0 wording that called Today the default home is
historical and does not override the route contract above.

## Known degraded or intentionally unproven

- Full `REAL_MODEL_VALIDATED` cannot be claimed before one complete, reproducible
  same-run natural-language full chain passes; the final run had timeline
  `source_quote` binding fluctuation, so current multi-run evidence remains
  `PARTIAL / DEGRADED`.
- Clean runtime/live, browser/390×844, served revision, exact-SHA CI and push are
  unverified for the current worktree; exact-SHA runtime, browser, owner acceptance
  and GitHub Actions remain `PENDING` before commit/push.
- The arm64 Mach-O package is only a `LOCAL PREVIEW`: it is ad-hoc local and not
  Developer ID signed, notarized, a DMG or a public release. Calendar's actual TCC
  user authorization is still pending even though the app metadata/entitlement is
  present.
- `14_DAY_USEFULNESS` and `USER_VALIDATED` have no evidence; no launch-worthiness
  or two-week usefulness conclusion is permitted.
- Feedback effects are bounded to non-authority timing/cooldown/suppression; a
  durable improvement in usefulness is not proven.
- P3 identity/relationship expansion and Phase 6 self-extension are outside the
  V1 gate; no future capability is implied by the automated green state.

## Next decision

Root should now independently establish clean runtime/restart, actual Calendar TCC
user authorization, browser/390×844, served revision and exact-SHA CI/push evidence;
the local package build itself is validated, but it remains a LOCAL PREVIEW. Resolve
the timeline `source_quote` binding fluctuation and rerun one complete same-run
natural-language acceptance once. Only
after that may the relevant `PENDING` or `PARTIAL / DEGRADED` labels
be reconsidered. A real two-week owner sample remains a separate product-
validation decision.
