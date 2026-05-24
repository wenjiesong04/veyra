# Veyra Development Status

Updated after P6 Core model-assisted reasoning hardening.

## Data Reality

The backend and console do not use hardcoded demo fixtures. The UI reads live local API responses from Veyra endpoints such as `/state`, `/runtime`, `/architecture`, `/logs/*`, `/agents`, `/agent/status`, and `/mvp/status`.

Current local `state/` data may contain older self-test records from earlier development. Those records are real runtime outputs from tests, not static mock data. Runtime JSONL logs and snapshots are intentionally excluded from commits unless explicitly requested. Current self-test scripts redirect `VEYRA_STATE_ROOT` and `VEYRA_AGENCY_ROOT` to temporary directories before app import, so new test traffic does not write into the working runtime state or the tracked agency intention queue.

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
| P6 End-to-end runtime hardening | Implemented, live validation pending | Task polling/stop/refresh, Agent result callback, ActionProposal hardening, Agent Tool Proxy contract verification, Core model-assisted reasoning, model-ranked memory, ExternalWorld refresh, Agency intentions, stale refresh, real probe envelopes, external-memory bridge slots, and provider diagnostics. API validation fields now separate implemented/configured/validated state. |
| P7 Production operations and safety validation | Implemented, live validation pending | Non-destructive red-team validation, retention summary/enforcement, runtime matrix, bounded/session soak APIs, Ops health, alerts, local/webhook alert dispatch, deployment config validation, and deployment readiness checks exist. `/ops/deployment` and `/ops/runtime-matrix` report not_configured or validation_pending instead of pretending live production readiness. |

## Eight Architecture Blocks

| Block from original design | Current implementation |
| --- | --- |
| Veyra Core | Implemented MVP core loop plus model-assisted reasoning for intent/route, foresight, memory relevance, ExternalWorld interpretation, perception, and agency gaps. P4 adds evidence-backed verification. |
| Interface Adapter / Agent Adapter | Implemented Intake/EventNormalizer plus AgentRegistry. OpenClaw, Hermes, and Custom adapters share the v1 task/result/capability contract. |
| Probe Tools | Implemented system, git, port, process, file, network, web, log, MCP, OpenClaw, Hermes probe modules with standardized result envelopes where wired. |
| Memory Bridge | Implemented local memory bridge for summary reads, model-assisted relevance ranking, filtered patch writes, provider routing (`local` / `selected` / runtime names / `all`), external adapter hooks, and provider diagnostics. Production external memory semantics still need live runtime validation. |
| Skill | Implemented registry/runtime and built-in skill definitions. Skills route through AwarenessLoop and now record execution trace. |
| Tool Proxy | Implemented SafeShell, SafeFile, SafeBrowser, SafeAPI with Guardian-style policy review, runtime executor config, host allowlists, standard tool trace, and Agent result bypass verification. |
| Rollback / Audit | Implemented snapshot, diff, restore, rollback log, policy trace, tool trace, execution trace, ActionJournal timeline, time-travel summary, non-destructive replay plans, and replay compensation review proposals. |
| Web Control UI | Implemented React/Vite console served at `/console`, backed by live Veyra APIs and built into `ui/console`. |

## Veyra Core Submodules

| Core module | Status | Notes |
| --- | --- | --- |
| Runtime Entity | P6 model-aware | Identity, lifecycle, selected agent, idle/thinking/acting/blocked states, redacted Core model runtime summary |
| Awareness Loop | P6 model-aware | Main event loop and route handling for direct answer, probe, skill, agent, review, block; injects Core reasoning outline into Agent task context |
| Attention Core | MVP implemented | Text-driven focus slice and context scoping |
| Belief & Uncertainty Core | MVP implemented | Claim confidence, TTL, stale/conflict summaries |
| WorldState | MVP implemented | Local JSON/JSONL state store and schema metadata |
| Core Model / Reasoning | P6 implemented | OpenAI-compatible Core model config/status, redacted traces, model-assisted decision/foresight/memory/external-world/perception/agency with rule fallback |
| Agency Core | P6 model-aware | Intention queue behavior exists; model can propose additional bounded state gaps, then Foresight/Guardian decides execute/suggest/review |
| Perception Layer | P6 model-aware | Probe interpretation into state/belief patches; model can add grounded claims and anomaly interpretation |
| Persona Engine | MVP implemented | Persona patch generation and operational mode display in console |
| Decision Core | P6 model-aware | Deterministic safety baseline plus model-assisted intent, complexity, route, solution outline, signals, constraints; risk cannot be lowered by model |
| Foresight Engine | P6 model-aware | Risk impact, reversibility, safer alternatives, preconditions, and unsafe assumptions; model can add caution but cannot weaken rule impact |
| Guardian / Execution Controller | MVP implemented | Risk policy, confirmation gate, block/allow decisions, tool proxy enforcement |
| Verifier | P4 completed | Evidence-backed verdicts and rollback/probe/memory next actions |
| Context / Patch Builder | MVP implemented | Context, policy, persona, task packet generation |

## Current Public Surfaces

| Surface | Purpose |
| --- | --- |
| `/runtime`, `/state`, `/heartbeat` | Runtime identity, state cache, heartbeat |
| `/architecture`, `/definitions`, `/mvp/status` | Architecture metadata, risk/lifecycle/mode definitions, implementation flags, and validation status |
| `/events/message` | Standard user-message event entry |
| `/core/model/status`, `/core/model/config`, `/logs/core-model` | Core model config/status and redacted model reasoning audit |
| `/memory/providers`, `/memory/providers/diagnostics`, `/memory/summary`, `/memory/patch` | Memory provider discovery, diagnostics, summary reads, and filtered writes across local/selected/explicit runtime providers |
| `/external/watchlist`, `/external/refresh` | Add/update ExternalWorld watch targets and refresh them through read-only probes |
| `/agent/contract`, `/agent/status`, `/agents`, `/agents/select`, `/agents/{name}/config` | Agent contract, Tool Proxy contract, status, selection, adapter configuration |
| `/actions/proposals`, `/reviews/*` | Action review and human confirmation flow |
| `/tool-proxy/*`, `/tool-proxy/status`, `/tool-proxy/config` | Safe shell/file/browser/API execution boundary with optional Browser/API executor configuration and host allowlists |
| `/rollback/*`, `/audit/journal`, `/audit/time-travel`, `/audit/replay/*` | Snapshot, diff, restore, git diff, correlated journal, time-travel summary, replay plans, and guarded replay compensation proposals |
| `/ops/health`, `/ops/alerts`, `/ops/alerts/dispatch`, `/ops/alerting`, `/ops/deployment`, `/ops/deployment/config`, `/ops/runtime-matrix`, `/ops/runtime-matrix/run`, `/ops/soak`, `/ops/soak/status`, `/ops/soak/start`, `/ops/soak/stop`, `/ops/safety/red-team`, `/ops/retention`, `/ops/retention/enforce` | Operational health, alerts, local/webhook alert dispatch, deployment readiness/config validation, runtime matrix with validation metadata, bounded/session soak, red-team safety, and retention checks/enforcement |
| `/logs/events`, `/logs/actions`, `/logs/tools`, `/logs/policy`, `/logs/execution`, `/logs/rollback`, `/logs/memory` | Audit and trace surfaces |
| `/console` | Awareness & Agent Control Console |

## Design Compliance

The current codebase follows the original design direction:

- Veyra is not a replacement Agent Runtime; it governs a selected runtime.
- Messages enter Veyra first; Veyra runs a deterministic safety baseline, optionally uses its Core model for understanding/planning, then applies Guardian before native execution or Agent delegation.
- Simple tasks are handled directly or through probes/skills.
- Complex Agent tasks receive state, background, policy, model-ranked memory, decision trace, foresight, and Core model solution outline through `VeyraTaskPacket.context_patch`.
- Agent tasks include `policy_patch.tool_proxy_contract`; Verifier flags high-risk `tool_calls` that lack ActionProposal, review, policy, or Tool Proxy trace evidence.
- High-risk actions go through Guardian, policy trace, review, and Tool Proxy.
- Execution results now require verifier evidence instead of blind trust.
- Rollback/Audit records snapshots, diffs, traces, and restores.
- The console is an Awareness & Agent Control Console rather than a plain chat box.

Remaining live validation gates:

- Runtime matrix exists for OpenClaw/Hermes/Custom; live multi-runtime soak validation still requires configured running runtimes.
- External Memory Bridge diagnostics exist, but production OpenClaw/Hermes memory semantics still need live runtime validation.
- Browser/API execution has configurable executor hooks and host allowlists; live production allowlists still need environment-specific validation.
- Replay compensation is implemented as review-backed snapshot restore; live side-effect replay beyond snapshot restore remains intentionally gated.
- Long-running soak now has controlled session APIs; alert delivery supports local audit log and optional configured webhook, with webhook delivery requiring a configured endpoint.
