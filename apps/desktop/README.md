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

From the repo root:

```bash
./scripts/start_desktop_dev.sh
```

The Tauri shell starts or reuses the local Veyra API automatically at `http://127.0.0.1:8000`.

## Build

From the repo root:

```bash
./scripts/build_desktop.sh
```

The build script creates the React desktop assets, builds a PyInstaller backend sidecar named `veyra-backend-<target-triple>`, and then runs `tauri build`. The resulting app package starts the bundled backend when the user opens `Veyra`.

Packaging prerequisites:

- Rust/Cargo and Node.js/npm
- Python dependencies installed with `./scripts/install_local.sh`
- PyInstaller installed in the active Python environment, for example `python3 -m pip install pyinstaller`

## Icons

`src-tauri/icons/icon-source-safe.png` is the canonical processed source icon. The original hand-cut source is not used by the app package.

Regenerate the packaged app icons from the processed source:

```bash
python3 scripts/generate_desktop_icons.py
```

The generator creates macOS, Windows, and Linux icons under `src-tauri/icons/`. The packaged app uses those processed icons, so rough source-image corners are not displayed at the system icon edge.
