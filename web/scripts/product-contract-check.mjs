import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(new URL("..", import.meta.url).pathname);
const read = (name) => readFileSync(resolve(root, name), "utf8");
const proxy = read("vite.proxy.ts");
const matters = read("src/matters.tsx");
const conversation = read("src/conversation.tsx");
const shared = read("src/shared.tsx");
const main = read("src/main.tsx");
const settings = read("src/settings.tsx");
const vite = read("vite.config.ts");
const desktopVite = read("vite.desktop.config.ts");

function objectBlock(source, key) {
  const marker = new RegExp(`\\b${key}\\s*:\\s*\\{`, "g");
  const match = marker.exec(source);
  if (!match) throw new Error(`Vite config is missing ${key} block`);
  const start = marker.lastIndex - 1;
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char;
      continue;
    }
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Vite config has an unterminated ${key} block`);
}

if (!proxy.includes('"/product": "http://127.0.0.1:8000"')) throw new Error("Vite proxy does not forward /product");
for (const config of [vite, desktopVite]) {
  for (const key of ["server", "preview"]) {
    if (!/\bproxy\s*:\s*apiProxy\b/.test(objectBlock(config, key))) {
      throw new Error(`Vite ${key} proxy contract is incomplete`);
    }
  }
}
for (const key of ["situations", "attention", "suggestions", "commitments", "questions", "waiting"]) {
  if (!matters.includes(`key: "${key}"`)) throw new Error(`Matters is missing ${key} section mapping`);
}
if (!matters.includes("value.count") || !matters.includes("value.items")) throw new Error("Matters does not consume the stable section contract");
if (!shared.includes("sanitizeHistoryRecord") || !shared.includes("sanitizeHistory(")) throw new Error("History sanitizer is not shared");
if (shared.includes("console-user") || shared.includes("console-session")) throw new Error("Shared product surface retains console scope defaults");
if (!main.includes("productContextRequest") || !main.includes("productContextRetries")) throw new Error("Product context request is not single-flight/bounded");
if (!main.includes("AbortController") || !main.includes("controller.abort()") || !main.includes("signal: controller.signal")) throw new Error("Product context request lacks timeout cancellation");
if (!conversation.includes('"Unknown"') || !conversation.includes("listText(situations[0].unknown)")) throw new Error("Today does not visibly project unknown situation evidence");
if (!settings.includes("Promise.allSettled") || !settings.includes("statusReadable")) throw new Error("Settings does not separate setup/status failure handling");
console.log("product frontend contract checks passed");
