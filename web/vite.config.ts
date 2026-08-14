import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { apiProxy } from "./vite.proxy";

export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    outDir: "../ui/console",
    emptyOutDir: true
  },
  server: {
    proxy: apiProxy
  },
  preview: {
    proxy: apiProxy
  }
});
