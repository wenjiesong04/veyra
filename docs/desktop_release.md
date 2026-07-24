# Veyra Desktop Release Plan

Veyra Desktop is the local window distribution for Veyra. The product name is `Veyra` on macOS, Windows, and Linux.

## Cross-Platform Strategy

Use one desktop shell and one console UI:

- Desktop shell: Tauri v2 under `apps/desktop`.
- Frontend: the existing React/Vite console, built with `web/vite.desktop.config.ts`.
- Backend: local FastAPI runtime on `127.0.0.1:8000`, auto-started by the desktop shell.
- Local setup API: `/setup/status` and `/setup/env` for first-run status and whitelisted local `.env` writes.

The web console build remains separate at `ui/console` with `/console/` asset paths. The desktop build writes relative assets into `apps/desktop/dist`, so a packaged window can load them without depending on the web route.

## Operating System Targets

| OS | User-facing package | Build host |
| --- | --- | --- |
| macOS | `Veyra.app` / `.dmg` | macOS |
| Windows | `Veyra.msi` / `.exe` | Windows |
| Linux | `Veyra.AppImage` / `.deb` | Linux |

Build on the target operating system first. Cross-compilation, code signing, notarization, Microsoft Store packaging, and Linux distro-specific signing are later release-engineering work.

## Current Scaffold

Implemented now:

- `apps/desktop` Tauri app named `Veyra`.
- `scripts/start_desktop_dev.sh` for local desktop development.
- `scripts/build_desktop.sh` for target-OS desktop package builds.
- `desktop_backend.py` as the bundled FastAPI backend entrypoint.
- `scripts/build_desktop_sidecar.py` builds the PyInstaller sidecar expected by Tauri.
- Tauri `bundle.externalBin` packages `veyra-backend-<target-triple>` with the app.
- `web/vite.desktop.config.ts` for desktop-safe relative assets.
- Local CORS allowlist for Tauri/local dev origins.
- `/setup/status` reports app/platform/path/agent/Feishu/deployment status and desktop backend mode.
- `/setup/env` writes only whitelisted `.env` keys from local clients and returns redacted values.
- The first-run wizard can install the explicitly pinned OpenClaw CLI version, optionally start the Gateway without blocking the API event loop, and recheck connectivity.
- Secret fields use password inputs; the Core model key is written to the same fixed environment variable referenced by the saved model configuration.
- Non-loopback API exposure requires `VEYRA_LOCAL_API_TOKEN`; setup/status and other control-plane reads are not public.
- `scripts/generate_desktop_icons.py` creates macOS/Windows/Linux icons from the processed source image so imperfect source corners are not shown at the display edge.

Validation still pending:

- Code signing and notarization.
- Windows/Linux package validation.
- Native cross-process state-writer locking on Windows; current status is explicitly `validation_pending`.
- Full first-run coverage for every advanced Tool Proxy and alert value.

## First-Run Flow

Target user flow:

1. User opens `Veyra`.
2. Veyra starts or finds the local backend.
3. The setup window checks local config, Core model, Agent runtime, Feishu, and deployment readiness.
4. If OpenClaw/Agent is missing, Veyra shows install/deploy guidance and retries detection.
5. User saves optional model, Agent, Feishu, Tool Proxy, and alert settings through the window.
6. Veyra runs `/health`, `/ops/deployment`, `/agent/status`, and a local `/events/message` acceptance.

The current implementation reaches steps 1-6 at code level. Release acceptance still requires target-OS signed package tests plus real OpenClaw and Feishu credentials.
