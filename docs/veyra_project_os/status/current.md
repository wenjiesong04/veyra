# Current Status

> Evidence snapshot date: 2026-08-22 (Asia/Shanghai)
>
> 本页是 Project OS 的 current Living truth。当前代码 checkpoint 为
> `34d44dfca0534f837b72b2ebe15949ed517ea699`；exact-SHA Actions run
> `32564052536` 已成功；重启后的 startup `runtime_build` 报告该 revision、
> `dirty=false`、`loaded_code_attested=false`，current served UI/browser validation
> 通过。source consent、用户
> 价值与 release 证据仍独立记录；未闭合的位置保持 `PENDING`，不稳定的真实模型
> 证据标为 `PARTIAL / DEGRADED`。

## Release class and evidence ladder

| Dimension | Current label | Boundary |
|---|---|---|
| Release class | `PRIVATE LOCAL PREVIEW CANDIDATE` | 本机 private/local preview candidate；不是 public stable release、公开发布或外部交付产品 |
| Implementation | `IMPLEMENTED` | V1 semantic/reaction/source/product paths exist in the shared worktree |
| Automated | `AUTOMATED_VALIDATED` | final local gate `166/166` (`145` invariant + `1` cognitive + `20` product), OpenClaw `32/32`, Route `810/810`, Web/Desktop build/product contract/bundle green; generic health/education/finance generalization smoke passed |
| Real model | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | isolated real-model asked-answer series `3/3`; long-session move update resolved mover Information Need while retaining network/date Unknowns. Broader natural-language model reliability remains partial/degraded; Jarvis-like route is boundedly demonstrated, not proven |
| Bounded live | `BOUNDED_LIVE: VALIDATED` | startup `runtime_build` reported `34d44df`, `dirty=false`, `loaded_code_attested=false`; current served UI and desktop/`390x844` browser validation passed; Calendar/Weather/Public Web consent and long-term value remain separate |
| Sustained usefulness | `SUSTAINED_USEFULNESS: PENDING` | no sustained 7–14 day owner sample, usefulness or quiet-rate thresholds |
| User validation | `USER_VALIDATED: PENDING` | owner acceptance and correction/timing evidence not closed |
| Authority | `RECORD_ONLY / NO EXTERNAL DELIVERY` | Agent research, Tool/Grant expansion and external delivery remain disabled |

## Current revision and worktree

- Branch: `cognitive-awakening`.
- Code checkpoint observed for this documentation pass:
  `34d44dfca0534f837b72b2ebe15949ed517ea699`.
- Exact-SHA Actions run `32564052536` succeeded:
  <https://github.com/wenjiesong04/veyra/actions/runs/32564052536>.
- Restarted startup `runtime_build` reported the code checkpoint with `dirty=false`
  and `loaded_code_attested=false`; current served UI/browser validation passed.
  This pass changes documentation only; it does not claim a clean docs worktree,
  loaded-code byte attestation or public delivery.

## V1-001 — Living Context Alpha

Status: `PRIVATE LOCAL PREVIEW CANDIDATE / IMPLEMENTED + AUTOMATED_VALIDATED / REAL_MODEL_VALIDATED PARTIAL / DEGRADED`

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
deterministic Calendar chain and bounded read paths for Weather and Public Web are
evidenced, while actual user consent remains pending. Agent research and external
delivery are disabled.

The final local checkpoint also passed generic health/education/finance
generalization smoke and exercised three original live Situations through the
same mechanism. A long-session move update resolved the mover Information Need
while retaining network/date Unknowns. These observations do not expand the
authority boundary.

### Product information architecture

The current product route contract is:

- `#/` — independent quiet First Meeting home;
- `#/today` — independent Today view, not the default home;
- `#/situations` and Situation detail — durable concerns, evidence, unknowns and
  reactions;
- `#/chat/<id>` — dedicated Chat for input, answers and corrections.
- `#/settings` — setup, source-consent and permission explanation surface.

Advanced/Developer Console remains a separate technical surface. A successful
build does not by itself prove a live browser result; the final local browser
review did accept Home/Today/Situations/Chat/Settings at desktop and `390x844`,
including Context gating, 404 UX, source labels and question dedupe.

### Evidence ledger

| Evidence position | Current state | What it supports | What it does not support |
|---|---|---|---|
| Python full gate | `AUTOMATED_VALIDATED` | final `166/166` = `145` invariant + `1` cognitive + `20` product | user value |
| OpenClaw / Route | `AUTOMATED_VALIDATED` | OpenClaw `32/32`; Route `810/810`; governance/non-regression boundaries | external connectivity or delivery |
| Web/Desktop build, product contract, bundle | `AUTOMATED_VALIDATED + LOCAL_BROWSER_VALIDATED` | frontend product-contract, status-tone, build, `check:bundle`, `build:desktop` and current served desktop/`390x844` browser acceptance all PASS; Context gating, 404 UX, source labels and question dedupe reviewed | source consent and sustained user value |
| Local desktop package | `LOCAL PREVIEW / AUTOMATED_VALIDATED` | clean local arm64 Mach-O package PASS; Python `3.11.15`, PyInstaller `6.22.0`, Rust `1.96.1`, Tauri `2.11.4`; ad-hoc signed; sidecar smoke, normalized sidecar match and `codesign --verify --deep --strict` PASS; sidecar SHA `818f8f34be54f9fb1ab1a088c5c800eaae588abf757bd62f8d0e21373f2cc106`, Veyra.app SHA `a5cd2dd483872f88d6a1f3b56f0b8ce2316293a74db1da1ff78c5415039667ab` | actual Calendar TCC and Weather/Public Web user consent; not Developer ID/notarized/DMG/public release |
| Moonshot natural input | `REAL_MODEL_VALIDATED: PARTIAL / DEGRADED` | isolated real-model asked-answer series `3/3`; long-session move update resolved mover Information Need and retained network/date Unknowns; broader natural-language reliability remains partial/degraded | full broad natural-language reliability, provider stability, long-term usefulness and Jarvis remain unproven |
| Calendar / Weather / Public Web | `IMPLEMENTED + AUTOMATED_VALIDATED + SOURCE_PATH_OBSERVED` | read-only source contracts and deterministic Calendar chain; bounded read paths are exercised | actual Calendar TCC and Weather/Public Web user consent |
| LivingReaction retention | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded archive/retention, replay and tamper/scope failure paths | long-term owner usage and operational soak |
| Feedback | `IMPLEMENTED + AUTOMATED_VALIDATED` | bounded timing/cooldown/suppression ledger behavior | long-term calibration or user usefulness |
| Clean runtime/restart | `BOUNDED_LIVE: VALIDATED` | startup `runtime_build` reported `34d44df`, `dirty=false`, `loaded_code_attested=false`; current served UI/runtime check passed | loaded-code byte attestation, Calendar/Weather/Public Web consent and sustained user value |
| Browser / 390×844 | `LOCAL_BROWSER_VALIDATED` | current served Home/Today/Situations/Chat/Settings accepted at desktop and `390x844`; Context gating, 404 UX, source labels and question dedupe passed | sustained user value and owner sign-off |
| Push / exact-SHA CI | `EXACT_SHA_CI: VALIDATED` | remote branch contained `34d44df`; run `32564052536` succeeded for that exact SHA: <https://github.com/wenjiesong04/veyra/actions/runs/32564052536> | public delivery/release; prior `77dbaf5` remains historical |
| 7–14 day usefulness / quiet-rate / owner validation | `PENDING` | no sustained owner sample, usefulness, quiet-rate, false-silence, wrong-timing or owner sign-off evidence | launch readiness and public release |

## V0-001 disposition

`V0-001 — Local Product Preview` is explicitly absorbed into V1-001 by the
owner-expanded scope. Its bounded product projection and historical local package
remain context, but it is not recorded as an independent V1 launch, Consumer V1,
or user-validated release. Older V0 wording that called Today the default home is
historical and does not override the route contract above.

## Known degraded or intentionally unproven

- The isolated real-model asked-answer series is `3/3`, but broader natural-language
  model reliability remains `PARTIAL / DEGRADED`; this is not a full broad-chain or
  Jarvis-like proof.
- Local desktop, exact-revision runtime and current served browser acceptance are
  validated independently. This does not grant Calendar TCC, Weather/Public Web
  user consent, sustained usefulness or public release authority.
- Current `/health` remains `critical` because of inherited Belief conflicts, stale
  Agent status and pending historical review items. The V1 API and Living Context
  loop are online; this is operational cleanup debt, not evidence of a clean public
  release.
- The arm64 Mach-O package is only a `LOCAL PREVIEW`: it is ad-hoc local and not
  Developer ID signed, notarized, a DMG or a public release. Calendar's actual TCC
  user authorization and Weather/Public Web user consent are still pending even
  though source contracts and app metadata/entitlement are present.
- Sustained 7–14 day usefulness/quiet-rate evidence and `USER_VALIDATED` have no
  evidence; a bounded private preview is supported, but no public-stable-release
  conclusion is permitted.
- Feedback effects are bounded to non-authority timing/cooldown/suppression; a
  durable improvement in usefulness is not proven.
- P3 identity/relationship expansion and Phase 6 self-extension are outside the
  V1 gate; no future capability is implied by the automated green state.

## Next decision

Root should now independently establish actual Calendar TCC and Weather/Public Web
user consent, sustained 7–14 day usefulness/quiet-rate, owner sign-off, and any
notarization/DMG/public-release evidence. Exact-revision runtime/browser and
exact-SHA Actions are validated for the bounded core product, but the artifact
remains a private/local preview candidate; no P1/P2 or public-release stage is
implied complete.
