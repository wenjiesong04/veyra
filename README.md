# Veyra

Virtual Entity for Yielding Real-time Awareness.

Veyra v0.1 is an awareness-first Agent governance runtime for local personal production. It receives events, updates local awareness state, evaluates risk, chooses a route, and either answers directly, runs a read-only probe, blocks unsafe actions, or generates a `VeyraTaskPacket` for the selected Agent Runtime.

Veyra is local-first: configuration, runtime state, and audit data stay on your machine under `.env`, `state/`, and `agency/`. When you enable a model, OpenClaw, Feishu, or another integration, Veyra sends the data required to serve that request to the configured provider. It is not a SaaS service and does not require Veyra cloud tenancy, accounts, or billing.

Veyra is now in Runtime Stabilization and local release hardening. The code-level awareness/governance loop is implemented, while external capabilities report `not_configured`, `validation_pending`, `validated`, `degraded`, or `stale` from observed evidence. Phase 3 now includes a live-validated, scoped OpenClaw pre-tool bridge for explicitly registered Veyra governed sessions. Broader native-tool coverage, cross-process recovery, real-workspace execution, production soak testing, native Memory validation, and fresh-clone/signed-package acceptance remain pending.

## Desktop Local Window

Veyra also has a first-stage desktop shell under `apps/desktop`. The software name is `Veyra` on macOS, Windows, and Linux. The desktop shell uses Tauri and reuses the same React/Vite console, but runs as a local application window instead of asking users to open a browser route manually.

Current desktop development flow:

```bash
./scripts/start_desktop_dev.sh
```

The desktop app starts or reuses the local API automatically on `127.0.0.1:8000`.

Build a desktop package on the target operating system:

```bash
./scripts/build_desktop.sh
```

The desktop package includes a local backend sidecar, so users can open `Veyra` without starting the API in a terminal. See [`docs/desktop_release.md`](docs/desktop_release.md).

## Five-Minute Local Start

Requirements:

- Python 3.11+
- Node.js/npm, only needed to rebuild the local console
- Optional: local OpenClaw Gateway at `http://127.0.0.1:18789`
- Optional: your own Feishu app credentials for local WebSocket or callback intake

```bash
git clone https://github.com/wenjiesong04/veyra.git
cd veyra
cp .env.example .env
./scripts/install_local.sh
```

Edit `.env` for only the services you use:

```bash
# Core model, OpenAI-compatible
VEYRA_CORE_MODEL_ENABLED=1
VEYRA_CORE_MODEL_BASE_URL=http://127.0.0.1:11434/v1
VEYRA_CORE_MODEL=your-model
VEYRA_CORE_MODEL_API_KEY=

# OpenClaw, optional but recommended
OPENCLAW_BASE_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=

# Feishu, optional
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_DEFAULT_RECEIVE_ID=
```

Start the API in the foreground:

```bash
./scripts/start_local.sh --foreground
```

On macOS, run it under the user LaunchAgent instead:

```bash
./scripts/start_local.sh --launchd
```

Check the local runtime:

```bash
./scripts/status_local.sh
curl -s http://127.0.0.1:8000/agent/status
```

Keep the API bound to loopback for the zero-configuration desktop flow. If `VEYRA_HOST` is set to a non-loopback address, configure `VEYRA_LOCAL_API_TOKEN`; only `/` and `/health` remain public, while setup, state, logs, and control endpoints require the token. Public provider callbacks are disabled unless explicitly enabled and independently verified.

Open the local control console after the frontend has been built:

```text
http://127.0.0.1:8000/console/
```

If you need a clean local runtime state:

```bash
./scripts/reset_local_state.sh
```

The reset script backs up `state/` under `.veyra-local-backups/` and recreates the default state files. It refuses to run while the local API is reachable unless `--force` is supplied.

## Local Data Boundary

Release files are code and templates only. User runtime data is local:

- `.env` contains local secrets and is ignored.
- `state/` contains runtime logs, snapshots, user commitments, channel state, OpenClaw device material, and memory mirrors; it is ignored.
- `agency/goals.json`, `agency/preferences.json`, `agency/triggers.yaml`, and `agency/self_policy.yaml` are generic defaults, not user history.
- Real Feishu/OpenClaw validation is environment-specific. Unconfigured services should report `not_configured` instead of pretending to be connected.

See [`docs/local_release_checklist.md`](docs/local_release_checklist.md) before cutting or publishing a local release.

## Current v0.1 Loop

```text
User/Event
  -> Intake Gateway
  -> VeyraEvent
  -> Awareness Loop
  -> Attention + Belief update
  -> Decision Core
  -> Foresight + Guardian
  -> Direct / Probe / Agent / Human Review / Block
  -> Verifier
  -> RuntimeTrace + ContextDrift telemetry
  -> WorldState + Audit update
```

## FastAPI

```bash
python3 -B -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Selected operational endpoints are listed below. The mechanically checked
206-product-route inventory, including Project Guardian, Cases, Tool
Governance, Phase 5, and Phase 6 control planes, is maintained in
[`docs/main_route_inventory.md`](docs/main_route_inventory.md).

- `GET /` runtime identity and status
- `POST /events/message` normalize a message and run the Awareness Loop
- `GET /channels` inspect local multi-channel intake state
- `GET /channels/{channel}/config` inspect a redacted channel configuration
- `POST /channels/{channel}/config` configure local or Feishu delivery
- `POST /channels/{channel}/messages` deliver a channel-scoped message with session/dedupe tracking
- `POST /channels/{channel}/send` send an outbound channel message directly
- `GET /channels/outbox` inspect local outbox delivery records
- `GET /channels/sessions` inspect channel session mappings
- `POST /integrations/feishu/events` receive Feishu URL verification and message callbacks
- `POST /integrations/feishu/import-openclaw` import Feishu app credentials from local OpenClaw config
- `GET /integrations/feishu/ws/status` inspect Feishu long-connection runner state
- `POST /integrations/feishu/ws/start` start Feishu WebSocket long-connection intake for local Veyra-first operation
- `GET /state` read current Veyra state cache
- `GET /architecture` read architecture blocks, core module progress, state definitions, and implementation phases
- `GET /definitions` read lifecycle statuses, operational modes, and risk-level catalog
- `GET /heartbeat` read the runtime heartbeat
- `GET /runtime` read runtime identity/lifecycle
- `GET /runtime/active-loop` inspect the continuous awareness loop
- `POST /runtime/active-loop/start` start bounded scheduled awareness ticks
- `POST /runtime/active-loop/stop` stop scheduled awareness ticks
- `POST /runtime/active-loop/tick` run one awareness tick immediately
- `GET /runtime/cron` inspect bounded runtime scheduler state
- `POST /runtime/cron/config` configure the active awareness scheduler job
- `POST /runtime/cron/run` run a scheduler job immediately
- `POST /runtime/cron/run-due` run due scheduler jobs
- `GET /runtime/traces/recent` inspect the latest routing traces, including real Feishu traffic
- `GET /runtime/traces/{trace_id}` inspect one routing trace
- `GET /runtime/soak/status` inspect Feishu runtime soak health from observed traces
- `GET /runtime/metrics/summary` inspect route distribution, latency, model/probe/agent counts, failures, and context size
- `GET /runtime/metrics/routes` inspect route counts and shares
- `GET /runtime/metrics/model-cost` inspect estimated token volume and model call counts
- `GET /runtime/metrics/failures` inspect recent runtime failures
- `GET /logs/events` read event log entries
- `GET /logs/actions` read action records
- `GET /reviews/actions` read blocked or review-needed actions
- `POST /reviews/{review_id}/approve` approve a pending human review item
- `POST /reviews/{review_id}/reject` reject a pending human review item
- `POST /tool-proxy/shell` execute a command through SafeShell policy
- `POST /tool-proxy/file/read` read a file through SafeFile policy
- `POST /tool-proxy/file/write` write a file with snapshot support
- `POST /tool-proxy/browser/open` review a browser-open request through SafeBrowser policy
- `POST /tool-proxy/api/request` review an API request through SafeAPI policy
- `GET /tool-proxy/status` inspect SafeShell/SafeFile/SafeBrowser/SafeAPI executor availability
- `POST /tool-proxy/config` enable/disable optional SafeBrowser/SafeAPI executors and host allowlists
- `GET /setup/status` inspect local desktop/setup status
- `POST /setup/env` write whitelisted local `.env` values from a local setup UI
- `GET /logs/policy` read policy trace records
- `POST /actions/proposals` submit an Agent or Tool action proposal through Guardian review
- `POST /rollback/snapshot` create a file snapshot
- `POST /rollback/{snapshot_id}/restore` restore a file snapshot
- `GET /audit/journal` inspect correlated event/action/tool/policy/execution/memory/model timeline
- `GET /audit/replay/{trace_id}` build a non-destructive replay plan
- `POST /audit/replay/{trace_id}/propose` turn a snapshot-backed replay plan into a guarded review item
- `GET /audit/replay/runtime/status` inspect automatic replay/compensation jobs
- `POST /audit/replay/runtime/scan` scan audit logs for replay candidates
- `POST /audit/replay/runtime/run` create guarded compensation review jobs for pending candidates
- `POST /audit/replay/runtime/config` configure guarded replay auto-execution gates
- `POST /audit/replay/runtime/execute` explicitly auto-approve and execute allowed snapshot restore jobs
- `GET /audit/time-travel` inspect last-known state from append-only audit logs
- `GET /agent/status` inspect the cached selected Agent status without probing the runtime
- `GET /agents/certification` inspect multi-runtime certification matrix
- `POST /agents/certification/run` certify OpenClaw/Hermes/Custom runtime surfaces
- `POST /agents/invoke` invoke one or more validated configured Agent runtimes
- `GET /agent/tasks/{task_id}?user_id=...&session_id=...` read the cached exact-owner Agent task status
- `POST /agent/tasks/{task_id}/refresh?user_id=...&session_id=...` explicitly poll one exact-owner Agent task
- `POST /agent/tasks/refresh` refresh all pending selected Agent tasks
- `POST /agent/tasks/{task_id}/stop?user_id=...&session_id=...` request an exact-owner Agent task stop
- `POST /agent/results` receive an Agent result callback and update verification state
- `GET /agent/contract` inspect the AgentAdapter v2 contract
- `GET /commitments?user_id=...&session_id=...` list one user's commitments, optionally narrowed to an exact session
- `POST /commitments` create a commitment with explicit `user_id` and `session_id` in the request body
- `GET /commitments/{commitment_id}?user_id=...&session_id=...` read one exact-owner commitment
- `POST /commitments/{commitment_id}/{confirm|pause|cancel}?user_id=...&session_id=...` mutate one exact-owner commitment
- `POST /commitments/run-due` run the explicitly operator-wide local commitment scheduler
- `GET /memory/summary?user_id=...&session_id=...` read a deterministic owner-scoped local Memory projection without model or external provider I/O
- `POST /memory/summary/resolve` explicitly run model-assisted and external-provider Memory resolution
- `GET /memory/providers` list available MemoryBridge providers
- `GET /memory/providers/diagnostics` read cached no-probe MemoryBridge diagnostics
- `POST /memory/providers/diagnostics` explicitly run active provider diagnostics with an optional write probe
- `POST /memory/patch` write owner/session-scoped caller-attested content; trust, verification, authority, quality, freshness, and storage provenance are server-owned
- `GET /belief/status` inspect claim TTL, freshness, conflicts, and source trust
- `POST /belief/refresh` refresh and optionally prune stale/expired claims
- `GET /agency/intentions` read proactive Agency intention queue
- `GET /personas/status` inspect current persona/channel/Agent binding state
- `POST /state/refresh-stale` refresh stale belief claims with read-only probes
- `POST /external/watchlist` add or update an exact owner/session ExternalWorld watch target
- `POST /external/refresh` refresh ExternalWorld watchlist targets with read-only probes
- `GET /ops/safety/red-team` run non-destructive safety validation cases
- `GET /ops/retention` inspect append-only log retention status
- `POST /ops/retention/enforce` archive and truncate logs that exceed retention limits
- `GET /ops/health` inspect runtime health and alert summary
- `GET /ops/alerts` list active operational alerts
- `POST /ops/alerts/dispatch` write alerts to local alert log and optionally configured webhook
- `GET /ops/alerting` inspect alert delivery configuration and recent dispatches
- `POST /ops/alerting/config` configure local/webhook alert delivery
- `GET /ops/deployment` inspect deployment readiness checks
- `GET /ops/deployment/config` inspect static deployment configuration validation
- `GET /ops/runtime-matrix` inspect last multi-runtime matrix result
- `POST /ops/runtime-matrix/run` check OpenClaw/Hermes/Custom connection, capability, memory, and task-status surfaces
- `POST /ops/soak` run a bounded operational health loop
- `GET /ops/soak/status` inspect the current soak session
- `POST /ops/soak/start` start a controlled soak session
- `POST /ops/soak/stop` request the active soak session to stop
- `GET /console/` open the Veyra control console after the frontend is built

Example:

```json
{
  "text": "帮我看 18789 端口有没有被占用",
  "channel": "webhook",
  "user_id": "local-user",
  "session_id": "local-session"
}
```

## CLI

```bash
python cli.py
```

## Web Control Console

The console follows the OpenClaw-style React + Vite control UI stack while keeping Veyra's backend on FastAPI.

```bash
cd web
npm install
npm run build
cd ..
python3 -B -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/console/
```

For frontend-only development:

```bash
cd web
npm run dev
```

The Vite dev server proxies API calls to `http://127.0.0.1:8000`.

## MVP Self-Test

Run the current gate smoke suite:

```bash
python3 scripts/run_smokes.py --group gate --timeout 90
python3 -m compileall awareness core decision execution foresight guardian interface memory_bridge probes rollback_audit routers runtime skills tool_proxy main.py cli.py desktop_backend.py scripts
```

Run the broader core governance loop, review approval, Tool Proxy, rollback, Memory Bridge, proactive check, and readiness checks in one command:

```bash
python3 scripts/mvp_self_test.py
```

Self-tests use temporary `VEYRA_STATE_ROOT` and `VEYRA_AGENCY_ROOT` directories so they do not pollute the local runtime state or tracked agency files.

## MVP Governance Workflow

Implementation progress and the current architecture map are tracked in
[`docs/implementation_progress.md`](docs/implementation_progress.md).
Route ownership and the first-stage `main.py` split are tracked in
[`docs/main_route_inventory.md`](docs/main_route_inventory.md).

```text
User message
  -> VeyraEvent
  -> Awareness Loop
  -> Attention + Belief update
  -> Decision Core
  -> Foresight + Guardian
  -> Direct / Probe / Agent / Human Review / Block
  -> Verifier
  -> State + Audit + Memory update
```

Human review flow:

```text
R3/R4 action
  -> Guardian ask_user
  -> state/review_queue.json
  -> Console Action Review
  -> Approve / Reject
  -> action_record.jsonl
```

Executable action proposal flow:

```text
Agent / Tool ActionProposal
  -> POST /actions/proposals
  -> R0-R2: execute through ActionExecutor
  -> R3-R4: write ReviewQueue and wait for approval
  -> Approve: execute proposal through SafeShell / SafeFile
  -> GuardianDecision + ToolTrace + Verifier + ActionRecord/Audit
  -> Reject: audit only, no execution
```

Agent flow:

```text
Complex task
  -> ContextPatch + PolicyPatch + PersonaPatch
  -> VeyraTaskPacket
  -> selected AgentAdapter
  -> ExecutionResult
  -> Verifier
  -> MemoryBridge
```

Continuous awareness flow:

```text
Scheduled tick / manual tick
  -> heartbeat
  -> pending Agent task refresh
  -> stale Belief TTL refresh
  -> proactive local/external probes
  -> ExternalWorld watchlist refresh
  -> automatic replay scan + guarded compensation review creation
  -> retention summary
  -> active_loop_state + audit record
```

Feishu channel setup:

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_DEFAULT_RECEIVE_ID=oc_xxx
export FEISHU_VERIFICATION_TOKEN=optional-callback-token
```

Then enable the channel:

```bash
curl -X POST http://127.0.0.1:8000/channels/feishu/config \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"delivery":"feishu","default_receive_id_type":"chat_id"}'
```

Set the Feishu event callback URL to `/integrations/feishu/events`. Veyra handles URL verification and `im.message.receive_v1` text events. If the app enables encrypted callbacks, decrypt at the edge first; Veyra currently rejects encrypted callback payloads instead of guessing.

For local development without a deployed server, use Feishu WebSocket long-connection mode instead of HTTP callbacks. Veyra can import an existing local OpenClaw Feishu app config:

```bash
curl -X POST http://127.0.0.1:8000/integrations/feishu/import-openclaw \
  -H 'Content-Type: application/json' \
  -d '{"enable":true}'
```

Then disable OpenClaw's direct Feishu handling and start Veyra's long connection:

```bash
curl -X POST http://127.0.0.1:8000/integrations/feishu/ws/start
```

Do not run OpenClaw and Veyra as independent Feishu message consumers for the same app unless OpenClaw is only forwarding to Veyra; otherwise messages can bypass Veyra governance or be processed twice.

Agent runtimes are selected through `state/agent_config.json` or the console Agent Runtime panel. The MVP ships a native OpenClaw Gateway adapter plus compatible HTTP adapters for Hermes and a Custom Agent endpoint. OpenClaw remains the default:

```bash
export OPENCLAW_BASE_URL=http://127.0.0.1:18789
export OPENCLAW_GATEWAY_TOKEN=optional-token
export OPENCLAW_SCOPES=operator.read,operator.write
export HERMES_BASE_URL=http://127.0.0.1:18889
export CUSTOM_AGENT_BASE_URL=http://127.0.0.1:18989
export VEYRA_CORE_MODEL_ENABLED=1
export VEYRA_CORE_MODEL_BASE_URL=http://127.0.0.1:11434/v1
export VEYRA_CORE_MODEL=your-model
export VEYRA_CORE_MODEL_API_KEY=optional-key
```

Useful endpoints:

- `GET /agents`
- `POST /agents/select`
- `POST /agents/{name}/config`
- `GET /agents/certification`
- `POST /agents/certification/run`
- `POST /agents/invoke`
- `GET /agent/status`
- `GET /core/model/status`
- `POST /core/model/config`
- `GET /logs/core-model`

Core model flow:

```text
User -> Veyra
  -> TurnContextBuilder minimal scoped context
  -> ContextDriftDetector warning/remediation
  -> CoreModelReasoning structured cognition decision
  -> VeyraController capability/governance gate
  -> direct answer / probe / skill / ask_user / selected AgentAdapter / block
  -> Verifier
  -> memory_policy: forget / short_term / long_term
  -> state update + response
```

The Core model is inside Veyra Core, not inside the selected Agent Runtime. It is the cognition center for understanding, route recommendation, freshness judgment, risk interpretation, memory policy, and capability requests, but it does not execute tools. The Controller executes only after checking the capability registry and Guardian constraints. Complex Agent tasks receive the Core model's solution outline, decision trace, foresight when required, executor state, model-ranked memory, and bounded context inside the `VeyraTaskPacket.context_patch`.

The same capability can also be attached while configuring a selected runtime with `POST /agents/{name}/config` by setting `use_model_for_core`, `model_base_url`, `model_api_key_env`, and `model`. Top-level `/core/model/config` takes precedence when explicitly enabled.

Agent tool governance is part of the task contract. Veyra adds `policy_patch.tool_proxy_contract` to every Agent task packet. R3-R4 tool actions must go through `/actions/proposals` and return approval evidence; R5 actions are blocked. The Verifier rejects or downgrades Agent success claims when high-risk `tool_calls` lack ActionProposal, review, policy, or Tool Proxy trace evidence. `/agent/status` may report `tool_proxy_enforced=true` only when the broker canary matches the current explicit implementation identity and a fresh Gateway snapshot confirms that the governance plugin is active with the expected protocol and revision. This is limited to `veyra_governed_openclaw_sessions`; unregistered sessions, cross-process recovery, and real-workspace execution are not covered.

Runtime observability now records each message route into `runtime_trace.jsonl` with redacted source identifiers, latency, final route, model/probe/agent usage, context size, drift warnings, memory policy, failure reason, and OpenClaw involvement. The telemetry APIs are backend-first so the console can consume them later without another route redesign.

`ContextDriftDetector` runs after `TurnContextBuilder` builds a compact turn packet and before Core reasoning consumes it. High drift scores remove stale beliefs, lower history/memory weight, compact context, and record `context_drift_log.jsonl` warnings.

MemoryBridge supports `local`, `selected`, explicit runtime names such as `openclaw` / `hermes` / `custom`, and `all` provider fan-out. Writes still pass the sensitive-memory filter before local storage or external adapter submission.

The Memory boundary is internal logical isolation for the loopback local runtime, not authenticated multi-tenancy. Agent bridge/task/history/tail/slot/ExternalWorld data requires exact `user_id + session_id`; profile/project/location/goal/commitment continuity is user-scoped. Belief/probe context, proactive planning, Agent continuation, commitment controls/watchlists/push, and intake dedupe use the same explicit owner boundary. API identity is still caller-declared, `/state` and several diagnostics remain operator-wide, and native OpenClaw Memory stays disabled until its scope contract is directly certified. Until then, the supported path is a Veyra-private owner/session-scoped mirror outside every OpenClaw workspace or configured memory index; an unsafe mirror path fails closed.

OpenClaw uses the same WebSocket Gateway protocol as the local OpenClaw Control UI. Veyra converts `http://127.0.0.1:18789` to `ws://127.0.0.1:18789`, sends `connect`, checks `health` / `status`, and submits Agent work with `chat.send`. If OpenClaw is reachable but requires device pairing or a gateway token, `/agent/status` reports that explicitly instead of treating the control UI HTML as a working Agent API.

For OpenClaw deployments with Control UI auth enabled, set `OPENCLAW_GATEWAY_TOKEN` to the dashboard token. If that environment variable is not set, Veyra can read the local dashboard token from `~/.openclaw/openclaw.json` at runtime; set `VEYRA_OPENCLAW_USE_LOCAL_CONFIG=0` to disable that fallback. Veyra stores its generated OpenClaw device identity in `state/local/openclaw_device.json` and ignores that file in git because it contains local signing material. The adapter writes this credential atomically with file mode `0600`; its standard `state/local` parent is restricted to `0700`, and an existing legacy file is tightened before it is read.
OpenClaw status and execution artifacts are redacted and summarized before being exposed through Veyra state endpoints, so gateway tokens, device tokens, signatures, private keys, host paths, and full runtime snapshots are not copied into `/agent/status` or audit logs.

Agent runtime version changes are handled at the adapter boundary. VeyraCore depends on the `AgentAdapter` contract, while `OpenClawAdapter` negotiates the gateway protocol, checks advertised methods, and soft-fails optional methods such as `tools.catalog` and `skills.status`. If OpenClaw raises its gateway protocol, set `OPENCLAW_PROTOCOL_MIN` / `OPENCLAW_PROTOCOL_MAX` before changing core code, then verify with `GET /agent/status` and `python3 scripts/mvp_self_test.py`. After an OpenClaw host/plugin version, provider/model/auth configuration, or governance revision change, rerun the real Phase 3 live canary; status checks and offline self-tests alone do not renew enforcement validation.

Hermes and Custom HTTP adapters expect these runtime endpoints by default:

- `POST /tasks`
- `GET /capabilities`
- `GET /memory/summary?session_id=...`
- `POST /memory/patch`
- `POST /tasks/{task_id}/stop`

Without a configured base URL, Veyra still builds the task packet but returns `adapter_unconfigured` instead of pretending that an Agent task was sent. Multi-Agent invocation requires at least one configured and certified runtime; unconfigured runtimes are skipped, R3/R4 requests become review-needed, and R5 requests are blocked.

## Implemented Architecture Slices

- `core`: Runtime Entity, Awareness Loop, active runtime loop, WorldState, Core model reasoning, Decision, Foresight, Guardian, Verifier, Patch/TaskPacket builders
- `awareness`: Attention, Belief TTL/source-trust lifecycle, Uncertainty, Awareness summary/output
- `interface`: Intake, event schema/normalizer, local multi-channel routing/dedupe/outbox, Feishu OpenAPI delivery/callback adapter, channel adapters, OpenClaw WebSocket adapter, and Hermes/Custom HTTP Agent adapters
- `probes`: system, git, port, process, file, log, network, web, MCP, OpenClaw, and Hermes read-only probe envelopes
- `tool_proxy`: SafeShell, SafeFile, SafeBrowser, and SafeAPI policy gates with trace logging and optional executor hooks
- `rollback_audit`: snapshot, diff, ActionJournal timeline, replay plan, automatic replay runtime, compensation review jobs, guarded auto-execute, traces
- `routers`: FastAPI route split for runtime observability, debug/audit/state, ops/runtime, agent/memory, Cases, commitments, Tool Governance, local setup, Phase 5, and Phase 6 surfaces; `main.py` retains runtime wiring plus 28 high-coupling product routes for intake/channel/Feishu, local Tool Proxy, rollback, health, MVP, and runtime entrypoints
- `memory_bridge`: local/selected/runtime/all provider routing, sensitive-memory filtering, provider diagnostics, and external adapter hooks
- `skills`: built-in skill registry/runtime for fixed low-risk workflows
- `personas`: Minimalist, Operator, Engineer, Guardian, Steward
- `state`: JSON/JSONL state cache and audit files

## Validation Semantics

Veyra reports implementation and validation separately. A module can be `implemented` while a live OpenClaw, Hermes, Custom Agent, memory provider, alert webhook, or production soak is still `not_configured` or `validation_pending`.

- `implemented`: code path exists and is covered by local checks.
- `configured`: an external runtime or executor has a concrete endpoint/configuration.
- `validated`: the real external surface passed the check specific to the claim; capability or status success alone does not validate tool enforcement.
- `validation_pending`: code and configuration exist, but the real external service did not yet pass live validation.

This keeps the API honest: Veyra does not fake a connected Agent runtime or production-ready deployment when the local environment has not provided one.
