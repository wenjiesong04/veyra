# main.py Route Inventory

Runtime Stabilization pass, 2026-05-26.

Before this pass, `main.py` held the route layer directly. The user-facing target referred to 106 existing routes; this pass adds 7 runtime observability/metrics routes, so the app now exposes 113 API routes total. First-stage split keeps core runtime construction in `main.py`, moves low-risk route surfaces into `routers/`, and leaves business objects unchanged.

Current `main.py` line count: 998.

## Router Split

| Router | Domains | Notes |
| --- | --- | --- |
| `main.py` | ui, feishu/channel intake, tool_proxy, rollback restore, ActionProposal, mvp/runtime status | Runtime object wiring remains centralized. |
| `routers/runtime_observability.py` | runtime, metrics | New trace/soak/telemetry APIs. |
| `routers/debug_audit.py` | state, debug, audit, rollback diff | Logs, model config, review queue, replay, stale refresh, external watch, belief/persona surfaces. |
| `routers/ops_runtime.py` | runtime, metrics/ops | Active loop, cron, soak, health, alerts, retention, deployment, runtime matrix. |
| `routers/agent_memory.py` | agent, memory | Agent task callbacks/status/config and memory provider routes. |

## Domain Classification

### ui

- `GET /`
- `GET /console`

### feishu

- `POST /integrations/feishu/events`
- `GET /integrations/feishu/ws/status`
- `POST /integrations/feishu/import-openclaw`
- `POST /integrations/feishu/ws/start`

### state

- `GET /state`
- `GET /architecture`
- `GET /definitions`
- `GET /heartbeat`
- `GET /agency/intentions`
- `GET /personas/status`
- `GET /belief/status`
- `POST /belief/refresh`
- `POST /state/refresh-stale`
- `POST /external/refresh`
- `POST /external/watchlist`

### runtime

- `GET /runtime`
- `GET /runtime/active-loop`
- `POST /runtime/active-loop/start`
- `POST /runtime/active-loop/stop`
- `POST /runtime/active-loop/tick`
- `GET /runtime/cron`
- `POST /runtime/cron/config`
- `POST /runtime/cron/run`
- `POST /runtime/cron/run-due`
- `GET /runtime/traces/recent`
- `GET /runtime/traces/{trace_id}`
- `GET /runtime/soak/status`
- `POST /ops/soak`
- `GET /ops/soak/status`
- `POST /ops/soak/start`
- `POST /ops/soak/stop`

### metrics

- `GET /runtime/metrics/summary`
- `GET /runtime/metrics/routes`
- `GET /runtime/metrics/model-cost`
- `GET /runtime/metrics/failures`
- `GET /ops/health`
- `GET /ops/alerts`
- `GET /ops/alerting`
- `POST /ops/alerting/config`
- `POST /ops/alerts/dispatch`
- `GET /ops/deployment`
- `GET /ops/deployment/config`
- `GET /ops/runtime-matrix`
- `POST /ops/runtime-matrix/run`
- `GET /ops/safety/red-team`
- `GET /ops/retention`
- `POST /ops/retention/enforce`

### debug

- `GET /core/model/status`
- `POST /core/model/config`
- `GET /logs/events`
- `GET /logs/actions`
- `GET /logs/tools`
- `GET /logs/policy`
- `GET /logs/execution`
- `GET /logs/rollback`
- `GET /logs/memory`
- `GET /logs/core-model`
- `GET /logs/alerts`
- `GET /mvp/status`

### agent

- `GET /agent/status`
- `GET /agent/contract`
- `GET /agent/tasks/{task_id}`
- `POST /agent/tasks/refresh`
- `POST /agent/results`
- `POST /agent/tasks/{task_id}/stop`
- `GET /agents`
- `GET /agents/certification`
- `POST /agents/certification/run`
- `POST /agents/invoke`
- `POST /agents/select`
- `POST /agents/{name}/config`

### memory

- `GET /memory/summary`
- `GET /memory/providers`
- `GET /memory/providers/diagnostics`
- `POST /memory/providers/diagnostics`
- `POST /memory/patch`

### rollback

- `POST /rollback/snapshot`
- `POST /rollback/{snapshot_id}/restore`
- `GET /rollback/{snapshot_id}/diff`
- `GET /rollback/diff`

### audit

- `GET /audit/journal`
- `GET /audit/time-travel`
- `GET /audit/replay/{trace_id}`
- `POST /audit/replay/{trace_id}/propose`
- `GET /audit/replay/event/{event_id}`
- `POST /audit/replay/event/{event_id}/propose`
- `GET /audit/replay/runtime/status`
- `POST /audit/replay/runtime/scan`
- `POST /audit/replay/runtime/run`
- `POST /audit/replay/runtime/config`
- `POST /audit/replay/runtime/execute`
- `GET /reviews/actions`
- `POST /reviews/{review_id}/approve`
- `POST /reviews/{review_id}/reject`

### tool_proxy

- `GET /tool-proxy/status`
- `POST /tool-proxy/config`
- `POST /tool-proxy/shell`
- `POST /tool-proxy/file/read`
- `POST /tool-proxy/file/write`
- `POST /tool-proxy/browser/open`
- `POST /tool-proxy/api/request`
- `POST /actions/proposals`

### channel

- `POST /events/message`
- `GET /channels`
- `GET /channels/{channel}/config`
- `POST /channels/{channel}/config`
- `POST /channels/{channel}/messages`
- `POST /channels/{channel}/send`
- `GET /channels/outbox`
- `GET /channels/sessions`
