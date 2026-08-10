# Veyra Console Build Output

The maintained React/Vite source lives in `web/`. FastAPI serves the checked-in
production build from `ui/console/` so a fresh local clone can open the Console
without a separate frontend development server.

Do not edit hashed files under `ui/console/assets/` by hand. Rebuild them from
the source:

```bash
cd web
npm ci
npm run build
npm run build:desktop
```

The previous per-panel placeholder directories were removed after those views
became real surfaces in `web/src/main.tsx`. Product status and future UI work are
tracked in `docs/README_Veyra.md`, not in generated assets or placeholder files.
