# V1-001 Acceptance Evidence — 2026-08-20

这是 V1 alpha 的独立 evidence ledger，不是最终发布报告。它把自动化、真实
模型、source、feedback、runtime/live、浏览器和 CI 分开记录；一类证据不能
替代另一类证据。

## Snapshot

| Field | Value |
|---|---|
| Release class | `PRIVATE LOCAL ALPHA` |
| Branch/base observed | `cognitive-awakening @ 7439aeb` |
| Worktree | V1 implementation changes uncommitted; final local-gate snapshot |
| Implementation | `IMPLEMENTED` |
| Automated | `AUTOMATED_VALIDATED` — final local gate `165/165` (`145` invariant + `1` cognitive + `19` product); Route `810/810`; OpenClaw `32/32` |
| Real model | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` |
| Bounded live | `BOUNDED_LIVE: PENDING` |
| Two-week usefulness | `14_DAY_USEFULNESS: PENDING` |
| Owner/user validation | `USER_VALIDATED: PENDING` |
| Authority | `RECORD_ONLY / NO EXTERNAL DELIVERY`; Agent research disabled |

## Product contract under test

The alpha uses one generic Living Context path for three non-hardcoded Situation
families:

1. travel, meeting or event arrangements;
2. job-search, application or study plans;
3. moving, personal-project or family plans.

The durable semantic projection includes Known, Unknown, Assumptions, timeline,
evidence, material change and Information Need. The governed reaction vocabulary
is `ask`, `read`, `wait`, `silent` and `suggest`; explanatory fields cover what
happened, why it matters, why now and the next step. Feedback is intentionally
limited to timing, cooldown and suppression. Calendar, Weather and Public Web are
read-only source classes. LivingReaction also has a bounded archive/retention
path for long-lived reaction and feedback history. Agent research, external
delivery and execution authority are disabled.

The route contract is deliberately separate:

- `#/` — quiet First Meeting home;
- `#/today` — Today, not the default home;
- `#/situations` and Situation detail;
- `#/chat/<id>` — dedicated Chat.

## Evidence positions

| Evidence class | Current label | Current observation | Still required |
|---|---|---|---|
| Python full gate | `AUTOMATED_VALIDATED` | `165/165` = `145` invariant + `1` cognitive + `19` product | exact-SHA CI remains separate |
| OpenClaw contract | `AUTOMATED_VALIDATED` | `32/32` | exact-SHA CI remains separate |
| Route governance | `AUTOMATED_VALIDATED` | `810/810` | exact-SHA CI remains separate |
| Web/Desktop build | `AUTOMATED_VALIDATED` | frontend product-contract, status-tone, build, `check:bundle` and `build:desktop` all PASS | browser and served-revision evidence |
| Local desktop package | `LOCAL PREVIEW / AUTOMATED_VALIDATED` | final local arm64 Mach-O package PASS; Python `3.11.15`, PyInstaller `6.22.0`, Rust `1.96.1`, Tauri `2.11.4`; sidecar smoke PASS, normalized cmp PASS, `codesign --verify --deep --strict` PASS; App `NSAppleEventsUsageDescription` and `automation.apple-events` entitlement present; sidecar SHA `471ef080f84639e8ca445fe20c91695095873a004854290008f6d1012304351`, Veyra.app tree SHA `0257a1b8b004cbb95deee158164bacdc5481d6921193169b04e5a30a570ab590` | actual Calendar TCC user authorization; ad-hoc local preview, not Developer ID/notarized/DMG/public release |
| Product contract/bundle | `AUTOMATED_VALIDATED` | product contract and bundle checks green | browser route/data acceptance |
| Moonshot natural input | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | multiple isolated runs together show three generic scenarios with create `3/3` once successful, update `3/3` once successful, Calendar update `1/1` once successful, and model-parsed/persisted `ignore feedback`; no single run completed the full chain. Final run: create `3/3`, update `2/3`, degraded by timeline `source_quote` binding fluctuation | no single-run full-chain PASS; provider stability, long-term usefulness and Jarvis remain unproven |
| Calendar | `IMPLEMENTED + AUTOMATED_VALIDATED + REAL_SOURCE_READ_OBSERVED` | deterministic full source→Need→understanding→reaction chain and a separate real provider read are evidenced | actual packaged-app TCC user authorization and current live restart |
| Weather | `IMPLEMENTED + AUTOMATED_VALIDATED + REAL_SOURCE_READ_OBSERVED` | read-only source boundary and current real read are evidenced | current served revision/live restart |
| Public Web | `IMPLEMENTED + AUTOMATED_VALIDATED + REAL_SOURCE_READ_OBSERVED` | read-only source boundary and current real read are evidenced | current served revision/live restart |
| Feedback | `IMPLEMENTED + AUTOMATED_VALIDATED` | feedback can record bounded timing/cooldown/suppression effects and survive the tested lifecycle | real-model feedback replay and long-term usefulness |
| LivingReaction retention | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded archive/retention, replay and tamper/scope failure paths are covered | long-term owner usage and operational soak |
| Clean runtime/restart | `BOUNDED_LIVE: PENDING` | no current-run proof recorded here | clean startup identity, restart and state purity |
| Browser/390×844 | `BOUNDED_LIVE: PENDING` | build is not browser acceptance | route separation, data contract and responsive review |
| Push/exact-SHA CI | `PENDING` | no final push or remote result claimed | final SHA, remote CI and handoff link |
| 14-day usefulness | `14_DAY_USEFULNESS: PENDING` | no two-week sample | usefulness, false silence, wrong timing, corrections and thresholds |
| Owner acceptance | `USER_VALIDATED: PENDING` | no owner sign-off recorded | explicit owner review of the alpha |

## Real-model note

The Moonshot evidence is meaningful but not a complete one-pass acceptance. Across
separate isolated runs, the merged evidence is three generic scenarios with create
`3/3` once successful, update `3/3` once successful, Calendar update `1/1` once
successful, and `ignore feedback` parsed by the model and persisted. These results
did not occur in one run, so no single run completed the full chain. The final run
was create `3/3`, update `2/3`, and remained `DEGRADED` because timeline
`source_quote` binding fluctuated. This supports a `PARTIAL / DEGRADED` label only;
it does not prove a single-run full-chain PASS, stable provider behavior, long-term
usefulness or a Jarvis-like relationship.

## Source and feedback boundaries

Source evidence means a server-governed, exact-scope, consent-aware, read-only
request with freshness/TTL and typed receipt. It does not grant a Tool, Agent,
Route, Risk or external-delivery capability. Feedback evidence means a scoped
ledger effect on reaction timing, cooldown or suppression; it does not prove that
the user found the result useful or that a long-term calibration improved.

## Unknown / not to claim

- No `REAL_MODEL_VALIDATED: FULL` until one complete, reproducible same-run
  natural-language full chain passes all required stages. The final run had timeline
  `source_quote` binding fluctuation; current multi-run evidence is explicitly
  `PARTIAL / DEGRADED`.
- No `BOUNDED_LIVE` until clean runtime, current source/live, browser and served
  revision evidence are independently recorded.
- No `14_DAY_USEFULNESS` or `USER_VALIDATED` from fixtures, smoke tests, build
  output, three model runs or a green gate.
- Local packaging is only an ad-hoc arm64 Mach-O `LOCAL PREVIEW`: it is not Developer
  ID signed, notarized, a DMG or a public release. The app's automation entitlement
  and `NSAppleEventsUsageDescription` do not mean Calendar TCC consent has been
  granted; actual Calendar TCC user authorization remains pending.
- Exact-SHA runtime, browser acceptance, owner acceptance and GitHub Actions remain
  `PENDING` before commit/push; local package and gate evidence do not close them.
- No claim of external delivery, Agent research, P3/Phase 6 completion, public
  release, notarization, or exact-SHA CI.

## Handoff gate

Root's next evidence update should replace only the applicable `PENDING` or
`PARTIAL / DEGRADED` positions after fresh verification. In particular, clean
runtime/restart, actual Calendar TCC authorization, browser/390×844, push/exact-SHA
CI and owner validation remain independent evidence positions. Until then this ledger remains
the authoritative 2026-08-20 record of what is implemented, what automation covers,
what the real model has demonstrated, and what is still unknown.
