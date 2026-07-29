# FastAPI Route Inventory

Runtime Stabilization, proactive cognition, and Phase 6 control-plane pass,
verified from an isolated import of `main.app` on 2026-07-29.

`main.py` still owns runtime object wiring and the highest-coupling route
surfaces. Lower-risk surfaces are split into `routers/`. The current app
contains 211 Starlette/FastAPI route objects: 206 callable product
`APIRoute`s, 4 framework OpenAPI/Swagger/Redoc routes, and the `/console`
static mount. The explicit `GET /console` bootstrap route is one of the 206
product routes and is distinct from that mount.

Current `main.py` line count: 1625.

## Router Split

| Router | Domains | Notes |
| --- | --- | --- |
| `main.py` | ui, feishu/channel intake, tool_proxy, rollback restore, ActionProposal, health, mvp/runtime status | Runtime object wiring remains centralized. |
| `routers/runtime_observability.py` | runtime, metrics | New trace/soak/telemetry APIs. |
| `routers/debug_audit.py` | state, capabilities, debug, audit, rollback diff | Logs, model config, review queue, replay, stale refresh, external watch, belief/persona/attention surfaces. |
| `routers/ops_runtime.py` | runtime, metrics/ops | Active loop, cron, soak, health, alerts, retention, deployment, runtime matrix, external-runtime and stale-review hygiene. |
| `routers/agent_memory.py` | agent, memory | Agent task callbacks/status/config and memory provider routes. |
| `routers/cases.py` | durable cases | Owner-scoped Case list/detail and strict lifecycle commands. |
| `routers/commitments.py` | commitments | Local user commitments and due-run push controls. |
| `routers/local_setup.py` | setup | Local desktop/setup status and whitelisted `.env` writes from local clients. |
| `routers/phase5.py` | Phase 5 | Read-only foresight/portfolio/provider surfaces, feedback, and private JSON sandbox canary. |
| `routers/phase6.py` | Phase 6 collaboration | Owner-scoped read-only collaboration control plane. |
| `routers/phase6_extensions.py` | Phase 6 specs | Private non-executing ExtensionSpec quarantine and lifecycle. |
| `routers/phase6_extension_artifacts.py` | Phase 6 artifacts | Private inert artifact quarantine and lifecycle. |
| `routers/phase6_extension_source_checks.py` | Phase 6 source checks | Non-executing syntax/fixed-AST source gate. |
| `routers/tool_governance.py` | governed tools | Scoped OpenClaw preflight/execute/observe/postflight and hook status. |

## Product Route Classification

The sections below inventory all 206 product routes. Selected
ownership-sensitive routes are annotated for clarity; consult the generated
OpenAPI schema for the complete query/body contract of every route. The four
framework documentation routes are intentionally excluded.

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
- `GET /setup/openclaw`
- `POST /setup/openclaw/install`
- `POST /setup/complete`
- `POST /setup/env`
- `POST /state/refresh-stale`
- `POST /external/refresh`
- `POST /external/watchlist`
- `GET /capabilities/snapshot`
- `POST /capabilities/refresh`

### runtime

- `GET /runtime`
- `GET /health`
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
- `POST /ops/retention/compact`
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
- `GET /logs/memory?user_id=...&session_id=...`
- `GET /logs/core-model`
- `GET /logs/alerts`
- `GET /mvp/status`
- `POST /proactive/check`
- `GET /proactive/self-heal/status`

### agent

- `GET /agent/status`
- `GET /agent/contract`
- `GET /agent/tasks/{task_id}?user_id=...&session_id=...`
- `POST /agent/tasks/{task_id}/refresh?user_id=...&session_id=...`
- `POST /agent/tasks/refresh`
- `POST /agent/results`
- `POST /agent/tasks/{task_id}/stop?user_id=...&session_id=...`
- `GET /agents`
- `GET /agents/certification`
- `POST /agents/certification/run`
- `POST /agents/invoke`
- `POST /agents/governance-canary`
- `POST /agents/select`
- `POST /agents/{name}/config`

### memory

- `GET /memory/summary?user_id=...&session_id=...`
- `POST /memory/summary/resolve`
- `GET /memory/providers`
- `GET /memory/providers/diagnostics`
- `POST /memory/providers/diagnostics`
- `POST /memory/patch`

### commitments

- `GET /commitments?user_id=...&session_id=...`
- `POST /commitments`
- `GET /commitments/{commitment_id}?user_id=...&session_id=...`
- `POST /commitments/{commitment_id}/confirm?user_id=...&session_id=...`
- `POST /commitments/{commitment_id}/pause?user_id=...&session_id=...`
- `POST /commitments/{commitment_id}/cancel?user_id=...&session_id=...`
- `POST /commitments/run-due` (operator-wide local scheduler)

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

### awareness and Project Guardian

- `GET /events/inbox`
- `GET /events/awareness/status`
- `POST /events/awareness/config`
- `GET /awareness/project-guardian/status`
- `POST /awareness/project-guardian/config`
- `POST /awareness/project-guardian/run-once`
- `GET /awareness/project-guardian/producers/status`
- `POST /awareness/project-guardian/producers/run-once`
- `POST /awareness/project-guardian/release-goals`
- `GET /awareness/project-guardian/release-goals`
- `POST /awareness/project-guardian/release-goals/{goal_id}/status`
- `POST /awareness/project-guardian/release-goals/{goal_id}/deployment-intent`
- `POST /awareness/project-guardian/release-goals/{goal_id}/attention-policy`
- `GET /awareness/project-guardian/attention/status`
- `POST /awareness/project-guardian/attention/run-once`
- `POST /awareness/project-guardian/attention/dismissals/{suppression_key}`
- `GET /awareness/project-guardian/attention/assessments`
- `GET /awareness/project-guardian/attention/general-situations`
- `GET /awareness/project-guardian/candidates`
- `GET /awareness/situations`
- `GET /awareness/situations/{situation_id}`

### tool_proxy

- `GET /tool-proxy/status`
- `POST /tool-proxy/config`
- `POST /tool-proxy/shell`
- `POST /tool-proxy/file/read`
- `POST /tool-proxy/file/write`
- `POST /tool-proxy/browser/open`
- `POST /tool-proxy/api/request`
- `POST /actions/proposals`

### governed tool bridge

- `GET /tool-governance/status`
- `GET /tool-governance/hook/status`
- `POST /tool-governance/preflight`
- `POST /tool-governance/postflight`
- `POST /tool-governance/hook/preflight`
- `POST /tool-governance/hook/execute`
- `POST /tool-governance/hook/observe`
- `POST /tool-governance/hook/canary/attest`

### channel

- `POST /events/message`
- `GET /channels`
- `GET /channels/{channel}/config`
- `POST /channels/{channel}/config`
- `POST /channels/{channel}/messages`
- `POST /channels/{channel}/send`
- `GET /channels/outbox`
- `GET /channels/sessions`

### durable cases

- `GET /cases`
- `GET /cases/{case_id}`
- `POST /cases/{case_id}/commands`

### Phase 5

- `GET /phase5/status`
- `GET /phase5/portfolio`
- `GET /phase5/foresight/contracts`
- `GET /phase5/foresight/status`
- `GET /phase5/providers/certification`
- `POST /phase5/feedback`
- `POST /phase5/sandbox/json-candidate`

### Phase 6 collaboration

- `GET /phase6/status`
- `GET /phase6/collaborations`
- `POST /phase6/collaborations`
- `GET /phase6/collaborations/{case_id}`
- `POST /phase6/collaborations/{case_id}/advance`
- `POST /phase6/collaborations/{case_id}/select`
- `POST /phase6/collaborations/{case_id}/cancel`

### Phase 6 ExtensionSpec quarantine

- `GET /phase6/extensions/status`
- `GET /phase6/extensions/specs`
- `POST /phase6/extensions/specs`
- `GET /phase6/extensions/specs/{candidate_id}`
- `GET /phase6/extensions/specs/{candidate_id}/integrity`
- `POST /phase6/extensions/specs/{candidate_id}/review`
- `POST /phase6/extensions/specs/{candidate_id}/revoke`

### Phase 6 inert artifact quarantine

- `GET /phase6/extensions/artifacts/status`
- `GET /phase6/extensions/artifacts`
- `POST /phase6/extensions/artifacts`
- `GET /phase6/extensions/artifacts/{artifact_id}`
- `GET /phase6/extensions/artifacts/{artifact_id}/integrity`
- `POST /phase6/extensions/artifacts/{artifact_id}/reject`
- `POST /phase6/extensions/artifacts/{artifact_id}/revoke`

### Phase 6 non-executing source checks

- `GET /phase6/extensions/source-checks/status`
- `GET /phase6/extensions/source-checks`
- `GET /phase6/extensions/source-checks/{check_id}`
- `GET /phase6/extensions/source-checks/{check_id}/integrity`
- `POST /phase6/extensions/artifacts/{artifact_id}/source-check`
