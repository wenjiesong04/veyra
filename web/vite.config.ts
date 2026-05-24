import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    outDir: "../ui/console",
    emptyOutDir: true
  },
  server: {
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
      "/audit": "http://127.0.0.1:8000"
    }
  }
});
