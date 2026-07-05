# main.py Route Inventory

Runtime Stabilization and local release hardening pass, 2026-07-05.

`main.py` still owns runtime object wiring and the highest-coupling route surfaces. Lower-risk surfaces have been split into `routers/`. The current app exposes 138 FastAPI routes total, including OpenAPI/Swagger/Redoc and the console static mount; 133 are callable product/API routes, plus the `/console` static mount.

Current `main.py` line count: 1192.

## Router Split

| Router | Domains | Notes |
| --- | --- | --- |
| `main.py` | ui, feishu/channel intake, tool_proxy, rollback restore, ActionProposal, health, mvp/runtime status | Runtime object wiring remains centralized. |
| `routers/runtime_observability.py` | runtime, metrics | New trace/soak/telemetry APIs. |
| `routers/debug_audit.py` | state, capabilities, debug, audit, rollback diff | Logs, model config, review queue, replay, stale refresh, external watch, belief/persona/attention surfaces. |
| `routers/ops_runtime.py` | runtime, metrics/ops | Active loop, cron, soak, health, alerts, retention, deployment, runtime matrix, external-runtime and stale-review hygiene. |
| `routers/agent_memory.py` | agent, memory | Agent task callbacks/status/config and memory provider routes. |
| `routers/commitments.py` | commitments | Local user commitments and due-run push controls. |
| `routers/local_setup.py` | setup | Local desktop/setup status and whitelisted `.env` writes from local clients. |

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
- `GET /state/health`
- `GET /architecture`
- `GET /definitions`
- `GET /heartbeat`
- `GET /agency/intentions`
- `GET /personas/status`
- `GET /belief/status`
- `GET /belief/stale`
- `GET /attention/active`
- `POST /belief/refresh`

### setup

- `GET /setup/status`
- `POST /setup/env`
- `POST /state/refresh-stale`
- `POST /external/refresh`
- `POST /external/watchlist`
- `GET /capabilities/snapshot`
- `POST /capabilities/refresh`

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
- `GET /ops/external-runtime`
- `POST /ops/external-runtime/probe`
- `GET /ops/safety/red-team`
- `GET /ops/retention`
- `POST /ops/retention/enforce`
- `GET /ops/reviews/diagnostic`
- `POST /ops/reviews/{review_id}/resolve`
- `POST /ops/reviews/{review_id}/archive`

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
- `POST /proactive/check`

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

### commitments

- `GET /commitments`
- `POST /commitments`
- `GET /commitments/{commitment_id}`
- `POST /commitments/{commitment_id}/confirm`
- `POST /commitments/{commitment_id}/pause`
- `POST /commitments/{commitment_id}/cancel`
- `POST /commitments/run-due`

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
