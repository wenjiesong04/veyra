import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../apps/desktop/dist",
    emptyOutDir: true
  },
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/events": "http://127.0.0.1:8000",
      "/state": "http://127.0.0.1:8000",
      "/heartbeat": "http://127.0.0.1:8000",
      "/logs": "http://127.0.0.1:8000",
      "/reviews": "http://127.0.0.1:8000",
      "/runtime": "http://127.0.0.1:8000",
      "/core": "http://127.0.0.1:8000",
      "/external": "http://127.0.0.1:8000",
      "/agent": "http://127.0.0.1:8000",
      "/agents": "http://127.0.0.1:8000",
      "/architecture": "http://127.0.0.1:8000",
      "/definitions": "http://127.0.0.1:8000",
      "/rollback": "http://127.0.0.1:8000",
      "/audit": "http://127.0.0.1:8000",
      "/ops": "http://127.0.0.1:8000",
      "/setup": "http://127.0.0.1:8000",
      "/channels": "http://127.0.0.1:8000",
      "/integrations": "http://127.0.0.1:8000",
      "/tool-proxy": "http://127.0.0.1:8000"
    }
  }
});
