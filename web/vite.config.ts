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
      "/runtime": "http://127.0.0.1:8000"
    }
  }
});
