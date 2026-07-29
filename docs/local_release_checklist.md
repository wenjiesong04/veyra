# Veyra Local Release Checklist

This release target is local-first: users clone Veyra from GitHub, configure their own local model, OpenClaw, and optional Feishu app, and keep runtime data under their local `state/` directory.

## Release Boundary

- Do not commit `.env`, `state/`, runtime logs, snapshots, OpenClaw device files, Feishu credentials, or user commitments.
- Keep `agency/goals.json` generic. User commitments belong in local `state/user/user_commitments.json`, not the release template.
- Treat Hermes and Custom runtimes as optional. They should report `not_configured` until the user provides endpoints.
- Keep the control console local operator UI. It is not a hosted SaaS dashboard.

## Fresh Clone Acceptance

```bash
install -m 600 .env.example .env
./scripts/install_local.sh
./scripts/start_local.sh --foreground
```

In another terminal:

```bash
./scripts/status_local.sh
curl -s http://127.0.0.1:8000/agent/status
```

Expected result:

- API is reachable.
- `/health` may be `ready` or `degraded`, but degraded alerts must identify local configuration or runtime-state causes.
- OpenClaw reports `validated` only when the user's local OpenClaw Gateway is actually reachable.
- Feishu reports `not_configured` or `stopped` until the user provides app credentials and starts WebSocket intake.

## Local Runtime Hygiene

Use read-only diagnostics first:

```bash
./scripts/status_local.sh
curl -s http://127.0.0.1:8000/ops/reviews/diagnostic
curl -s http://127.0.0.1:8000/belief/status
```

For old pending reviews, use the existing guarded endpoints:

```bash
curl -X POST http://127.0.0.1:8000/ops/reviews/{review_id}/archive \
  -H 'Content-Type: application/json' \
  -d '{"reason":"local release hygiene: stale review"}'
```

For stale beliefs, prefer refresh before pruning:

```bash
curl -X POST http://127.0.0.1:8000/belief/refresh \
  -H 'Content-Type: application/json' \
  -d '{"expire_after_seconds":3600,"prune_expired_after_seconds":null}'
```

If the stale beliefs are old event/probe residue and you intentionally want a clean local release state, prune expired claims:

```bash
curl -X POST http://127.0.0.1:8000/belief/refresh \
  -H 'Content-Type: application/json' \
  -d '{"expire_after_seconds":0,"prune_expired_after_seconds":0}'
```

## Feishu / OpenClaw Soak

After configuring Feishu and OpenClaw locally:

1. Start Veyra with `./scripts/start_local.sh --launchd` on macOS or `--foreground` elsewhere.
2. Confirm `/agent/status` shows OpenClaw `validated`.
3. Confirm `/integrations/feishu/ws/status` has `configured=true`, `connected=true`, and a current-run `last_connected_at` after `/integrations/feishu/ws/start`; `thread_alive=true` alone is only a reconnect worker.
4. If claiming full Feishu E2E, send a real message and require current-run `last_processed_after_start=true`, `last_reply_sent_after_start=true`, no unrecovered processing failure, a websocket runtime trace, and a `provider_sent` outbox record with an external message id.
5. Send real Feishu messages covering direct answer, time/weather probe, safe Agent handoff, commitment confirm/cancel/status, and duplicate intake.
6. Inspect `/runtime/traces/recent`, `/runtime/soak/status`, and `/runtime/metrics/summary`.

Do not mark a release ready until live traces show the intended routes without uncontrolled tool execution.
