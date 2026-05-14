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
- `POST /rollback/snapshot` create a file snapshot
- `POST /rollback/{snapshot_id}/restore` restore a file snapshot
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

Agent flow:

```text
Complex task
  -> ContextPatch + PolicyPatch + PersonaPatch
  -> VeyraTaskPacket
  -> OpenClawAdapter
  -> ExecutionResult
  -> Verifier
  -> MemoryBridge
```

OpenClaw is connected only when `OPENCLAW_BASE_URL` is set:

```bash
export OPENCLAW_BASE_URL=http://127.0.0.1:18789
export OPENCLAW_API_KEY=optional-token
```

The adapter expects these runtime endpoints:

- `POST /tasks`
- `GET /capabilities`
- `GET /memory/summary?session_id=...`
- `POST /memory/patch`
- `POST /tasks/{task_id}/stop`

Without `OPENCLAW_BASE_URL`, Veyra still builds the task packet but returns `adapter_unconfigured` instead of pretending that an Agent task was sent.

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
