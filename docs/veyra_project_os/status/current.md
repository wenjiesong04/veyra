# Current Status

> Evidence snapshot date: 2026-08-14 (Asia/Shanghai)
>
> 本页是 Project OS 的 current Living truth，不是 North Star。历史 live 不能自动继承到新 revision；提交、推送和 exact-SHA Actions 仍需独立核验。

## Current revision and worktree

- Final application evidence revision: `e5fcf80ad3e1e6e54f3602603bdf941e5aa8fd37` (`e5fcf80`)；Product Preview code slice is in its parent feature changeset `80ff937`.
- `runtime v2` clean-start evidence is bound to `e5fcf80`: Python 3.11.15, `dirty_flag=false`, exact build identity, and business-state GETs remained byte-pure. This is revision-scoped evidence, not a claim that the shared worktree is currently clean.
- A clean final package on `e5fcf80` is validated. Push and exact-SHA GitHub Actions remain `PENDING`.

## V0-001 — Local Product Preview

Status: `LOCAL_VALIDATED / OWNER_ACCEPTANCE_PENDING`

Veyra 0.1 is a local-first Product Preview, not Consumer V1 and not a public or externally delivering release. The default surface is Today-first: exact-scope context, current Situation/Attention, record-only suggestion preview, honest unknown/waiting state, curated Status, and a continuing conversation. Advanced remains the old operator console.

### Evidence recorded for `e5fcf80`

| Surface | Evidence | Honest label |
|---|---|---|
| Python gate | `148/148` = `145` invariant + `1` cognitive capability + `2` product capability | `AUTOMATED_VALIDATED` |
| Route / governance | 9-route non-regression `810/810`; authority and record-only boundaries unchanged | `AUTOMATED_VALIDATED` |
| OpenClaw | plugin contract `32/32` | `AUTOMATED_VALIDATED` |
| Product backend | exact owner/session context, ambiguity/mismatch fail-closed, stable Matters sections, GET byte-purity, record-only preview and SSE lifecycle smoke | `AUTOMATED_VALIDATED` |
| Web / desktop / browser | Web build, Desktop frontend build, browser acceptance, 390×844 readability, shared history sanitizer and `/product` contract | `AUTOMATED_VALIDATED + BOUNDED LIVE` |
| Conversation | real SSE phases `accepted → phase → message → completed`; duplicate/rejected/failed are typed and no token/CoT stream is claimed | `AUTOMATED_VALIDATED` |
| Local app | clean `build_desktop.sh package` exit `0`; ad-hoc Apple Silicon arm64 `.app`; bundled sidecar is Mach-O arm64, Python 3.11.15, PyInstaller 6.22.0; sidecar smoke, strict codesign and normalized payload comparison completed. App tree SHA-256 `c62ac2a0c69865278ecc4096ac69105442e985c0aec0ee5b3c5f3ae0666abeca`; sidecar SHA-256 `a996f308d0c18443238c646c143f1966b66d8944b1ed707dc46be33264c95501` | `LOCAL_VALIDATED / NOT NOTARIZED` |
| Remote gate | final branch push and exact-SHA Actions | must be verified externally before handoff; never self-attested by this SHA-producing document |
| Public release | Developer ID, notarization, DMG/store distribution | `OUT OF SCOPE / NOT VALIDATED` |

### Known degraded or intentionally unproven

- P1 and P2 remain `PARTIAL`; the Workspace Observer is a bounded trusted canary, not a multi-source or general-life producer.
- User usefulness, timing, false silence and feedback aftereffect are not validated. The next planned slice is `LC-001 — Real non-code Situation`; it is not the current task while V0-001 remains release-evidence-pending.
- `ask` remains dormant; `record_only` is the default and external delivery, Agent/Tool execution, Route/Risk changes and new authority remain disabled.
- Generic cognition remains overconservative, production Economy metadata is incomplete, and no durable server conversation store is promised.
- Feishu/OpenClaw connectivity and any external delivery claim require a fresh current-run message; local Product Preview evidence does not imply that proof.

## Current evidence by dimension

| Dimension | Current label | Boundary |
|---|---|---|
| Implementation | `IMPLEMENTED IN BOUNDED V0.1 SLICE` | Product read model/router and Today-first UI exist; they do not replace underlying state truth. |
| Configuration | `LOCAL / LOOPBACK-TAURI` | Non-loopback remains subject to the existing local control-token policy. |
| Verification | `AUTOMATED_VALIDATED + BOUNDED LIVE` | Application evidence is revision-scoped to `e5fcf80`; the final branch SHA must be checked against GitHub Actions at handoff. |
| Runtime | `DEGRADED WHERE SOURCES ARE UNKNOWN` | Unknown freshness/readiness is shown as unknown, never upgraded to production-ready. |
| Authority | `RECORD_ONLY / NO EXTERNAL DELIVERY` | Product GETs and previews cannot grant execution, tool, route, risk or delivery authority. |

## Next decision

Do not mark V0-001 complete or archive it as `LC-001`. The clean local package is closed, while final remote-gate evidence belongs in the GitHub handoff rather than this self-referential document. After that gate passes, V0-001 still waits for owner acceptance of the local preview. Only an explicit owner decision may close it and open the planned real non-code `LC-001` Situation slice.
