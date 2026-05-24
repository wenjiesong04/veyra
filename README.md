# Veyra

Virtual Entity for Yielding Real-time Awareness.

Veyra v0.1 is an awareness-first Agent governance skeleton. It receives events, updates local awareness state, evaluates risk, chooses a route, and either answers directly, runs a read-only probe, blocks unsafe actions, or generates a `VeyraTaskPacket` for the selected Agent Runtime.

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
  -> WorldState + Audit update
```

## FastAPI

```bash
uvicorn main:app --reload
```

Endpoints:

- `GET /` runtime identity and status
- `POST /events/message` normalize a message and run the Awareness Loop
- `GET /state` read current Veyra state cache
- `GET /architecture` read architecture blocks, core module progress, state definitions, and implementation phases
- `GET /definitions` read lifecycle statuses, operational modes, and risk-level catalog
- `GET /heartbeat` read the runtime heartbeat
- `GET /runtime` read runtime identity/lifecycle
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
- `GET /logs/policy` read policy trace records
- `POST /actions/proposals` submit an Agent or Tool action proposal through Guardian review
- `POST /rollback/snapshot` create a file snapshot
- `POST /rollback/{snapshot_id}/restore` restore a file snapshot
- `GET /audit/journal` inspect correlated event/action/tool/policy/execution/memory/model timeline
- `GET /audit/replay/{trace_id}` build a non-destructive replay plan
- `GET /audit/time-travel` inspect last-known state from append-only audit logs
- `GET /agent/status` inspect selected Agent adapter connection status
- `GET /agent/tasks/{task_id}` poll selected Agent task status
- `POST /agent/tasks/refresh` refresh all pending selected Agent tasks
- `POST /agent/tasks/{task_id}/stop` request selected Agent task stop
- `POST /agent/results` receive an Agent result callback and update verification state
- `GET /agent/contract` inspect the AgentAdapter v1 contract
- `GET /memory/summary` read local Memory Bridge summary
- `GET /memory/providers` list available MemoryBridge providers
- `GET /agency/intentions` read proactive Agency intention queue
- `POST /state/refresh-stale` refresh stale belief claims with read-only probes
- `POST /external/watchlist` add or update an ExternalWorld watch target
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
- `POST /ops/soak` run a bounded operational health loop
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

Run the core governance loop, review approval, Tool Proxy, rollback, Memory Bridge, proactive check, and readiness checks in one command:

```bash
python3 scripts/mvp_self_test.py
```

## MVP Governance Workflow

Implementation progress and the current architecture map are tracked in
[`docs/implementation_progress.md`](docs/implementation_progress.md).

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
  -> ToolTrace + ActionRecord
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
- `GET /agent/status`
- `GET /core/model/status`
- `POST /core/model/config`
- `GET /logs/core-model`

Core model flow:

```text
User -> Veyra
  -> deterministic safety baseline + state snapshot
  -> optional Core model reasoning for intent, route, foresight, perception, agency gaps, and solution outline
  -> deterministic risk clamp + Foresight + Guardian
  -> direct answer / probe / skill / selected AgentAdapter
  -> verifier + model-ranked memory + perception / external world state update
```

The Core model is inside Veyra Core, not inside the selected Agent Runtime. It can improve understanding, planning, impact prediction, memory relevance, ExternalWorld interpretation, and precondition discovery, but it cannot lower a rule-detected risk level or bypass Guardian. Complex Agent tasks receive the Core model's solution outline, decision trace, foresight, executor state, model-ranked memory, and bounded context inside the `VeyraTaskPacket.context_patch`.

The same capability can also be attached while configuring a selected runtime with `POST /agents/{name}/config` by setting `use_model_for_core`, `model_base_url`, `model_api_key_env`, and `model`. Top-level `/core/model/config` takes precedence when explicitly enabled.

Agent tool governance is part of the task contract. Veyra adds `policy_patch.tool_proxy_contract` to every Agent task packet. R3-R4 tool actions must go through `/actions/proposals` and return approval evidence; R5 actions are blocked. The Verifier rejects or downgrades Agent success claims when high-risk `tool_calls` lack ActionProposal, review, policy, or Tool Proxy trace evidence.

MemoryBridge supports `local`, `selected`, explicit runtime names such as `openclaw` / `hermes` / `custom`, and `all` provider fan-out. Writes still pass the sensitive-memory filter before local storage or external adapter submission.

OpenClaw uses the same WebSocket Gateway protocol as the local OpenClaw Control UI. Veyra converts `http://127.0.0.1:18789` to `ws://127.0.0.1:18789`, sends `connect`, checks `health` / `status`, and submits Agent work with `chat.send`. If OpenClaw is reachable but requires device pairing or a gateway token, `/agent/status` reports that explicitly instead of treating the control UI HTML as a working Agent API.

For OpenClaw deployments with Control UI auth enabled, set `OPENCLAW_GATEWAY_TOKEN` to the dashboard token. If that environment variable is not set, Veyra can read the local dashboard token from `~/.openclaw/openclaw.json` at runtime; set `VEYRA_OPENCLAW_USE_LOCAL_CONFIG=0` to disable that fallback. Veyra stores its generated OpenClaw device identity in `state/openclaw_device.json` and ignores that file in git because it contains local signing material.
OpenClaw status and execution artifacts are redacted and summarized before being exposed through Veyra state endpoints, so gateway tokens, device tokens, signatures, private keys, host paths, and full runtime snapshots are not copied into `/agent/status` or audit logs.

Agent runtime version changes are handled at the adapter boundary. VeyraCore depends on the `AgentAdapter` contract, while `OpenClawAdapter` negotiates the gateway protocol, checks advertised methods, and soft-fails optional methods such as `tools.catalog` and `skills.status`. If OpenClaw raises its gateway protocol, set `OPENCLAW_PROTOCOL_MIN` / `OPENCLAW_PROTOCOL_MAX` before changing core code, then verify with `GET /agent/status` and `python3 scripts/mvp_self_test.py`.

Hermes and Custom HTTP adapters expect these runtime endpoints by default:

- `POST /tasks`
- `GET /capabilities`
- `GET /memory/summary?session_id=...`
- `POST /memory/patch`
- `POST /tasks/{task_id}/stop`

Without a configured base URL, Veyra still builds the task packet but returns `adapter_unconfigured` instead of pretending that an Agent task was sent.

## Implemented Architecture Slices

- `core`: Runtime Entity, Awareness Loop, WorldState, Core model reasoning, Decision, Foresight, Guardian, Verifier, Patch/TaskPacket builders
- `awareness`: Attention, Belief, Uncertainty, Awareness summary/output
- `interface`: Intake, event schema/normalizer, channel and Agent adapter interfaces
- `probes`: system, git, port, process, file, OpenClaw/Hermes placeholders
- `tool_proxy`: SafeShell, SafeFile, SafeBrowser/SafeAPI placeholders
- `rollback_audit`: snapshot, diff, ActionJournal timeline, replay plan, compensation plan, traces
- `memory_bridge`: memory read/write/filter placeholders
- `skills`: built-in skill registry placeholders
- `personas`: Minimalist, Operator, Engineer, Guardian, Steward
- `state`: JSON/JSONL state cache and audit files
