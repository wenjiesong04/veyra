import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { apiProxy } from "./vite.proxy";

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
    proxy: apiProxy
  },
  preview: {
    proxy: apiProxy
  }
});
