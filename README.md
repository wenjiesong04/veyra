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
- `POST /actions/proposals` submit an Agent or Tool action proposal through Guardian review
- `POST /rollback/snapshot` create a file snapshot
- `POST /rollback/{snapshot_id}/restore` restore a file snapshot
- `GET /agent/status` inspect selected Agent adapter connection status
- `GET /memory/summary` read local Memory Bridge summary
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
```

Useful endpoints:

- `GET /agents`
- `POST /agents/select`
- `POST /agents/{name}/config`
- `GET /agent/status`

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

- `core`: Runtime Entity, Awareness Loop, WorldState, Decision, Foresight, Guardian, Verifier, Patch/TaskPacket builders
- `awareness`: Attention, Belief, Uncertainty, Awareness summary/output
- `interface`: Intake, event schema/normalizer, channel and Agent adapter interfaces
- `probes`: system, git, port, process, file, OpenClaw/Hermes placeholders
- `tool_proxy`: SafeShell, SafeFile, SafeBrowser/SafeAPI placeholders
- `rollback_audit`: snapshot, diff, journal, trace placeholders
- `memory_bridge`: memory read/write/filter placeholders
- `skills`: built-in skill registry placeholders
- `personas`: Minimalist, Operator, Engineer, Guardian, Steward
- `state`: JSON/JSONL state cache and audit files
