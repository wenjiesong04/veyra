# Veyra OpenClaw governance plugin

This plugin narrows an explicitly registered OpenClaw dispatch to an exact
subset of three Veyra-backed tools:

- `veyra_file_read`
- `veyra_file_write`
- `veyra_shell_probe`

The subset may be empty; Phase 6 read-only collaboration deliberately uses an
empty allowlist. The plugin never reads, writes, or starts a process itself.
While an exact governed registration or its token-free tombstone is present,
every other tool is blocked and every allowed call must obtain a single-use
reservation from Veyra before its tool body can run. Sessions not registered by
Veyra keep OpenClaw's normal native-tool behavior, while the three `veyra_*`
tools always fail closed without registration.

## Install

```sh
openclaw plugins install --link /absolute/path/to/apps/openclaw/veyra-governance
```

The default Veyra origin is `http://127.0.0.1:8000`. It can be changed with the
plugin's `baseUrl` setting. Dispatch credentials are accepted only through the
Gateway method `veyra.governance.registerSession`. They exist briefly in the
OpenClaw host process, including a namespaced same-process compatibility store,
but are not written to durable Veyra/OpenClaw state or returned by status APIs.
That namespace prevents key collisions; it is not a security boundary from the
OpenClaw host or other same-process plugins, which are part of the TCB.
Credentials are removed on consumption, their own reservation expiry, session
expiry, exact cancellation, governance failure, registry retirement, or the
host's exact run `end`/`error` event.
Registry retirement leaves a token-free failed tombstone so a still-running old
governed session cannot fall back to native tools.
If a hook arrives without any usable session or run identity while a governed
marker exists, the plugin conservatively blocks it; this can also block an
identityless ordinary call until the governed run reaches a terminal event.

## Gateway contract

`veyra.governance.registerSession` accepts:

```json
{
  "runId": "run-id",
  "sessionKey": "agent:main:agent-exec:task-id",
  "dispatchToken": "opaque-bearer-token",
  "expiresAt": "2026-07-27T08:30:00Z",
  "bindingDigest": "64-lowercase-hex-characters",
  "allowedTools": [
    "veyra_file_read",
    "veyra_file_write",
    "veyra_shell_probe"
  ]
}
```

`expiresAt` must be no more than 15 minutes in the future.

The plugin calls:

- `POST /tool-governance/hook/preflight`, which must return either
  `{"allow": false, "reason": "..."}` or an allowed response containing
  `canonicalToolName`, `invocationDigest`, `reservationId`,
  `reservationToken`, `executionToken`, and `expiresAt`.
- `POST /tool-governance/hook/execute`, which must consume the execution token
  once and return `status`, `result`, `resultDigest`, `receiptRef`, and an
  optional `effectEvidenceDigest`.
- `POST /tool-governance/hook/observe`, which must return
  `{"status": "recorded", "authoritative": false}`.

`allowedTools` must be a duplicate-free subset of the three registered custom
tools. The Veyra broker binds the exact subset to the private execution profile;
the plugin can only preserve or restrict that set and never widens it.

The dispatch token is sent only in `X-Veyra-Dispatch-Token`; hook request bodies
do not contain it. Observation payloads contain bounded digests, and an
`after_tool_call` event is never promoted into execution evidence by this
plugin. Blocks of native and unknown tools are also reported as
non-authoritative `blocked` observations.

Use `veyra.governance.status` for aggregate counters and
`veyra.governance.cancelSession` with the exact `sessionKey`, `runId`, and
`bindingDigest` returned at registration to erase an active dispatch. Repeating
that exact cancellation is idempotent; a delayed cancellation for an older run
cannot cancel a replacement run that reused the same session key.

The plugin mirrors active run context through the host API and a same-process
compatibility store because Gateway RPC, tool hooks, and tool factories can be
served by different registry instances. When both mirrors exist they must be
byte-equivalent canonical data; disagreement fails closed, so a stale writable
host mirror cannot revive a process tombstone. A scoped reset retires only its
exact session and preserves the other observed bindings for a later restart.
A registry restart fails every exact governed run observed by the retiring
registry closed and retains its token-free marker; the run must be registered
again. An exact lifecycle `end`/`error` event removes the marker and all
process-local bearer material. There is no cross-process or host-restart
recovery.

## Test

```sh
npm test
```

The tests use only Node's built-in test runner and a local mock HTTP server.
They cover identity mismatch, missing run IDs, multi-registry bridging,
authority isolation, one-use and independently expiring reservations, exact
cancellation, scoped lifecycle cleanup, restart tombstones, mirror
disagreement, and terminal credential erasure. They are offline evidence only:
current enforcement additionally requires a real governed canary plus a fresh
Gateway status whose plugin protocol and implementation revision match the
broker attestation. Any host/plugin version, provider/model/auth configuration,
or governance revision change requires a fresh live canary.
