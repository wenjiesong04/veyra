# Veyra Development Status

Updated after P5 Web Control Console completeness.

## Data Reality

The backend and console do not use hardcoded demo fixtures. The UI reads live local API responses from Veyra endpoints such as `/state`, `/runtime`, `/architecture`, `/logs/*`, `/agents`, `/agent/status`, and `/mvp/status`.

Current local `state/` data may contain self-test records because `scripts/mvp_self_test.py` has been run repeatedly during development. Those records are real runtime outputs from tests, not static mock data. Runtime JSONL logs and snapshots are intentionally excluded from commits unless explicitly requested.

| Data area | Source | Reality |
| --- | --- | --- |
| Runtime / lifecycle | `RuntimeEntity` and `/runtime` | Real process-local runtime state |
| WorldState | `state/*.json` via `WorldStateStore` | Real local state cache, currently includes development/test updates |
| Event/action/tool/policy/execution logs | `state/*.jsonl` | Real append-only runtime logs, currently includes self-test traffic |
| OpenClaw/Hermes/Custom agent status | Adapter probes and `/agents` | Real configured adapter status; unconfigured adapters report that state |
| Console UI data | Backend API fetches | Live API data, no seeded fake dashboard objects |

## Phase Progress

| Phase | Status | Implemented result |
| --- | --- | --- |
| P0 Foundation definitions | Completed | Lifecycle, operational modes, risk levels R0-R5, Guardian decisions, architecture metadata |
| P1 State and probe hardening | Completed | Belief/Uncertainty TTL, probe envelope, perception state patches |
| P2 Decision / Guardian / Tool Proxy policy depth | Completed | Decision trace, Guardian policy patch, SafeShell/File/Browser/API policy trace |
| P3 Agent adapter execution contracts | Completed | `veyra.agent_adapter.v1`, OpenClaw WebSocket adapter, Hermes/Custom HTTP adapter, compatibility negotiation |
| P4 Rollback / Audit / Verifier depth | Completed | Verifier verdicts, execution trace, tool trace, rollback checksum/diff/restore evidence |
| P5 Web Control Console completeness | Completed | Console surfaces for setup, awareness, runtime, review, persona, state/logs, tool proxy, rollback/audit |
| P6 End-to-end runtime hardening | Pending | Real multi-runtime soak tests, long-task polling, failure recovery |
| P7 Production operations and safety validation | Pending | Red-team safety tests, deployment hardening, monitoring/retention policy |

## Eight Architecture Blocks

| Block from original design | Current implementation |
| --- | --- |
| Veyra Core | Implemented MVP core loop: Sense, Understand, Focus, Evaluate, Decide, Act, Verify, Update. P4 adds evidence-backed verification. |
| Interface Adapter / Agent Adapter | Implemented Intake/EventNormalizer plus AgentRegistry. OpenClaw, Hermes, and Custom adapters share the v1 task/result/capability contract. |
| Probe Tools | Implemented system, git, port, process, file, network, web, log, MCP, OpenClaw, Hermes probe modules with standardized result envelopes where wired. |
| Memory Bridge | Implemented local memory bridge for summary reads and filtered patch writes. Full external Agent Memory bridge remains future work. |
| Skill | Implemented registry/runtime and built-in skill definitions. Skills route through AwarenessLoop and now record execution trace. |
| Tool Proxy | Implemented SafeShell, SafeFile, SafeBrowser, SafeAPI with Guardian-style policy review, policy trace, and standard tool trace. |
| Rollback / Audit | Implemented snapshot, diff, restore, rollback log, policy trace, tool trace, execution trace. Replay/time-travel remains future work. |
| Web Control UI | Implemented React/Vite console served at `/console`, backed by live Veyra APIs and built into `ui/console`. |

## Veyra Core Submodules

| Core module | Status | Notes |
| --- | --- | --- |
| Runtime Entity | MVP implemented | Identity, lifecycle, selected agent, idle/thinking/acting/blocked states |
| Awareness Loop | MVP implemented | Main event loop and route handling for direct answer, probe, skill, agent, review, block |
| Attention Core | MVP implemented | Text-driven focus slice and context scoping |
| Belief & Uncertainty Core | MVP implemented | Claim confidence, TTL, stale/conflict summaries |
| WorldState | MVP implemented | Local JSON/JSONL state store and schema metadata |
| Agency Core | Placeholder/MVP | Proactive read-only check exists; full intention queue behavior remains future work |
| Perception Layer | MVP implemented | Probe interpretation into state/belief patches |
| Persona Engine | MVP implemented | Persona patch generation and operational mode display in console |
| Decision Core | MVP implemented | Intent, complexity, risk, capability, route, signals, constraints |
| Foresight Engine | MVP implemented | Risk impact, reversibility, safer alternatives for text/action review |
| Guardian / Execution Controller | MVP implemented | Risk policy, confirmation gate, block/allow decisions, tool proxy enforcement |
| Verifier | P4 completed | Evidence-backed verdicts and rollback/probe/memory next actions |
| Context / Patch Builder | MVP implemented | Context, policy, persona, task packet generation |

## Current Public Surfaces

| Surface | Purpose |
| --- | --- |
| `/runtime`, `/state`, `/heartbeat` | Runtime identity, state cache, heartbeat |
| `/architecture`, `/definitions`, `/mvp/status` | Architecture metadata, risk/lifecycle/mode definitions, readiness flags |
| `/events/message` | Standard user-message event entry |
| `/agent/contract`, `/agent/status`, `/agents`, `/agents/select`, `/agents/{name}/config` | Agent contract, status, selection, adapter configuration |
| `/actions/proposals`, `/reviews/*` | Action review and human confirmation flow |
| `/tool-proxy/*` | Safe shell/file/browser/API execution boundary |
| `/rollback/*` | Snapshot, diff, restore, git diff |
| `/logs/events`, `/logs/actions`, `/logs/tools`, `/logs/policy`, `/logs/execution`, `/logs/rollback`, `/logs/memory` | Audit and trace surfaces |
| `/console` | Awareness & Agent Control Console |

## Design Compliance

The current codebase follows the original design direction:

- Veyra is not a replacement Agent Runtime; it governs a selected runtime.
- Messages enter Veyra first; Agent execution is selected by Decision Core.
- Simple tasks are handled directly or through probes/skills.
- High-risk actions go through Guardian, policy trace, review, and Tool Proxy.
- Execution results now require verifier evidence instead of blind trust.
- Rollback/Audit records snapshots, diffs, traces, and restores.
- The console is an Awareness & Agent Control Console rather than a plain chat box.

Remaining gaps before calling it a complete Veyra runtime:

- Full Agency Core intention queue and long-running stewardship are not complete.
- Real external Memory Bridge adapters are not complete.
- Browser/API execution is policy-reviewed but not wired to real external executors by default.
- Replayable Agent Runtime and time-travel debugging are not implemented.
- P6/P7 production hardening, red-team safety tests, and deployment monitoring remain pending.
