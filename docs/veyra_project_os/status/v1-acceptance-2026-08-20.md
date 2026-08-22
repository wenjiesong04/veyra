# V1-001 Acceptance Evidence — 2026-08-20 (updated 2026-08-22)

这是 V1 alpha 的独立 evidence ledger，不是最终发布报告。它把自动化、真实
模型、source、feedback、runtime/live、浏览器和 CI 分开记录；一类证据不能
替代另一类证据。

> Current code checkpoint: `34d44dfca0534f837b72b2ebe15949ed517ea699`. Exact-SHA
> Actions run `32564052536` succeeded; restarted startup `runtime_build` reported
> the exact revision, `dirty=false` and `loaded_code_attested=false`, and current
> served UI/browser validation passed.
> Historical checkpoints remain explicitly identified at the end.

## Snapshot

| Field | Value |
|---|---|
| Release class | `PRIVATE LOCAL PREVIEW CANDIDATE` |
| Branch/base observed | `cognitive-awakening @ 34d44dfca0534f837b72b2ebe15949ed517ea699` |
| Worktree | final code checkpoint observed; this pass is docs-only and does not claim docs-tree cleanliness |
| Implementation | `IMPLEMENTED` |
| Automated | `AUTOMATED_VALIDATED` — final local gate `166/166` (`145` invariant + `1` cognitive + `20` product); Route `810/810`; OpenClaw `32/32`; generic health/education/finance generalization smoke passed; three original live Situations exercised on one mechanism |
| Real model | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` |
| Bounded live | `BOUNDED_LIVE: VALIDATED` — startup `runtime_build` reported the exact revision, `dirty=false` and `loaded_code_attested=false`; current served UI/browser validation passed; source consent and long-term value remain separate |
| Sustained usefulness | `SUSTAINED_USEFULNESS: PENDING` — sustained 7–14 day usefulness/quiet-rate evidence is absent |
| Owner/user validation | `USER_VALIDATED: PENDING` |
| Authority | `RECORD_ONLY / NO EXTERNAL DELIVERY`; Agent research disabled; unchanged |

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
- `#/settings` — source consent, setup and permission explanations.

The final local product review also covered Context gating, 404 UX, source labels
and question dedupe at desktop and `390x844`.

## Evidence positions

| Evidence class | Current label | Current observation | Still required |
|---|---|---|---|
| Python full gate | `AUTOMATED_VALIDATED` | `166/166` = `145` invariant + `1` cognitive + `20` product; exact-SHA Actions run `32564052536` succeeded for `34d44df` | public delivery/release |
| OpenClaw contract | `AUTOMATED_VALIDATED` | `32/32`; exact-SHA Actions run `32564052536` succeeded | public delivery/release |
| Route governance | `AUTOMATED_VALIDATED` | `810/810`; exact-SHA Actions run `32564052536` succeeded | public delivery/release |
| Web/Desktop build | `AUTOMATED_VALIDATED + LOCAL_BROWSER_VALIDATED` | frontend product-contract, status-tone, build, `check:bundle`, `build:desktop` and current served Home/Today/Situations/Chat/Settings browser acceptance at desktop/`390x844` all PASS; Context gating, 404 UX, source labels and question dedupe reviewed | source consent and sustained user value |
| Local desktop package | `LOCAL PREVIEW / AUTOMATED_VALIDATED` | clean local arm64 Mach-O package PASS; Python `3.11.15`, PyInstaller `6.22.0`, Rust `1.96.1`, Tauri `2.11.4`; ad-hoc signed; sidecar smoke, normalized sidecar match, `codesign --verify --deep --strict` PASS; sidecar SHA `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`, Veyra.app SHA `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab` | actual Calendar TCC and Weather/Public Web user consent; not Developer ID/notarized/DMG/public release |
| Product contract/bundle | `AUTOMATED_VALIDATED` | product contract and bundle checks green on the exact checkpoint | source consent and sustained user value |
| Moonshot natural input | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | isolated real-model asked-answer series `3/3`; long-session move update resolved mover Information Need while retaining network/date Unknowns; broader natural-language reliability remains partial/degraded | full broad natural-language reliability, provider stability, long-term usefulness and Jarvis remain unproven |
| Calendar | `IMPLEMENTED + AUTOMATED_VALIDATED + SOURCE_PATH_OBSERVED` | deterministic full source→Need→understanding→reaction chain and read-path evidence are recorded | actual packaged-app Calendar TCC user authorization |
| Weather | `IMPLEMENTED + AUTOMATED_VALIDATED + SOURCE_PATH_OBSERVED` | read-only source boundary/read path is recorded | Weather user consent |
| Public Web | `IMPLEMENTED + AUTOMATED_VALIDATED + SOURCE_PATH_OBSERVED` | read-only source boundary/read path is recorded | Public Web user consent |
| Feedback | `IMPLEMENTED + AUTOMATED_VALIDATED` | feedback can record bounded timing/cooldown/suppression effects and survive the tested lifecycle | real-model feedback replay and long-term usefulness |
| LivingReaction retention | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded archive/retention, replay and tamper/scope failure paths are covered | long-term owner usage and operational soak |
| Clean runtime/restart | `BOUNDED_LIVE: VALIDATED` | startup `runtime_build` reported `34d44df`, `dirty=false`, `loaded_code_attested=false`; current served runtime/UI check passed | loaded-code byte attestation, source consent and sustained user value |
| Browser/390×844 | `LOCAL_BROWSER_VALIDATED` | current served Home/Today/Situations/Chat/Settings accepted; Context gating, 404 UX, source labels and question dedupe passed | sustained user value and owner sign-off |
| Push/exact-SHA CI | `EXACT_SHA_CI: VALIDATED` | remote branch contained `34d44df`; run `32564052536` succeeded for that exact SHA: <https://github.com/wenjiesong04/veyra/actions/runs/32564052536> | public delivery/release |
| 7–14 day usefulness / quiet-rate | `SUSTAINED_USEFULNESS: PENDING` | no sustained owner sample or quiet-rate/usefulness thresholds | usefulness, false silence, wrong timing, corrections and launch readiness |
| Owner acceptance | `USER_VALIDATED: PENDING` | no owner sign-off recorded | explicit owner review of the alpha |

## Real-model note

The final isolated real-model asked-answer series is `3/3`. A long-session move
update resolved the mover Information Need while retaining network/date Unknowns,
which is bounded evidence for correction and epistemic retention. Broader
natural-language model reliability remains `PARTIAL / DEGRADED`; this does not prove
a stable provider, a complete broad natural-language chain, long-term usefulness or
a Jarvis-like relationship. The earlier create/update/source-quote fluctuation
results remain historical evidence, not a promotion of the current label.

## Source and feedback boundaries

Source evidence means a server-governed, exact-scope, consent-aware, read-only
request with freshness/TTL and typed receipt. Calendar real TCC and Weather/Public
Web user consent remain pending; a source path or contract check is not consent.
It does not grant a Tool, Agent, Route, Risk or external-delivery capability. Feedback evidence means a scoped
ledger effect on reaction timing, cooldown or suppression; it does not prove that
the user found the result useful or that a long-term calibration improved.

## Unknown / not to claim

- The asked-answer series `3/3` does not promote broader natural-language model
  reliability beyond `PARTIAL / DEGRADED`; no stable-provider or Jarvis-like claim.
- `BOUNDED_LIVE` is validated for the core exact-revision runtime/current-served
  UI/browser path. Calendar TCC and Weather/Public Web user consent remain
  independently pending.
- No `SUSTAINED_USEFULNESS` or `USER_VALIDATED` from fixtures, smoke tests, build
  output, the asked-answer series or a green gate; sustained 7–14 day usefulness/
  quiet-rate evidence remains pending.
- Local packaging is only an ad-hoc arm64 Mach-O `LOCAL PREVIEW`: it is not Developer
  ID signed, notarized, a DMG or a public release. The app's automation entitlement
  and `NSAppleEventsUsageDescription` do not mean Calendar TCC or Weather/Public Web
  user consent has been granted; those remain pending.
- Owner acceptance remains `PENDING`; exact-SHA runtime/current served browser and
  34d44df GitHub Actions are validated by run `32564052536`.
- Current `/health` remains `critical` because of inherited Belief conflicts, stale
  Agent status and pending historical review items; the V1 API and Living Context
  loop remain online. No claim is made of external delivery, Agent research,
  P3/Phase 6 completion, public stable release or notarization; V1 remains a
  private/local preview candidate.

## Handoff gate

Root's next evidence update should replace only the applicable `PENDING` or
`PARTIAL / DEGRADED` positions after fresh verification. In particular, actual
Calendar TCC and Weather/Public Web user consent, sustained 7–14 day usefulness/
quiet-rate, owner sign-off, and notarization/DMG/public-release evidence remain
independent positions. Exact-revision runtime/current served browser and the
34d44df exact-SHA Actions result are validated for the bounded core product.
This ledger remains the authoritative record of what is implemented, what
automation covers, what the real model has demonstrated, and what is still unknown.

## Historical 2026-08-20 checkpoint retained

The prior snapshot recorded local gate `165/165` (`145` invariant + `1` cognitive +
`19` product), old package hashes `471ef080…4351` and `0257a1…ab590`, and a
broader natural-language run that remained `PARTIAL / DEGRADED` after create `3/3`,
update `2/3` and timeline `source_quote` binding fluctuation. It also left browser
acceptance pending. Those values are preserved as historical evidence only; the
current code checkpoint and evidence are the 2026-08-22 values above.
