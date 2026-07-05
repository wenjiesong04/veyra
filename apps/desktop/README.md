# Veyra Desktop

Veyra Desktop is the local window shell for the Veyra personal runtime.

The product name is `Veyra` on every supported operating system. The desktop shell uses Tauri and reuses the React/Vite console from `web/`; it is not a hosted web app and does not move user state out of the local machine.

## Support Matrix

| OS | Package target | Status |
| --- | --- | --- |
| macOS | `.app` / `.dmg` | first supported release target |
| Windows | `.msi` / `.exe` | configured, needs Windows runner validation |
| Linux | `.AppImage` / `.deb` | configured, needs Linux runner validation |

Build release packages on the target operating system first. Cross-compilation and signing/notarization are release-engineering tasks, not part of the first launcher scaffold.

## Development

Start the local Veyra API in one terminal:

```bash
./scripts/start_local.sh --foreground
```

Start the desktop shell in another terminal:

```bash
cd apps/desktop
npm install
npm run dev
```

## Build

From the repo root:

```bash
./scripts/build_desktop.sh
```

Or from the desktop package:

```bash
cd apps/desktop
npm install
npm run build
```

The current scaffold expects the local Veyra API at `http://127.0.0.1:8000`. The next packaging phase should add a signed Python backend sidecar so end users can double-click `Veyra` without opening a terminal.
