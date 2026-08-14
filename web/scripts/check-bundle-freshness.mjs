import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(new URL("..", import.meta.url).pathname);
const bundle = resolve(root, "..", "ui", "console");
const htmlPath = resolve(bundle, "index.html");
if (!existsSync(htmlPath)) throw new Error("ui/console/index.html is missing; run npm run build first");
const html = readFileSync(htmlPath, "utf8");
const assetRefs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1]).filter((ref) => ref.includes("assets/"));
for (const ref of assetRefs) {
  const target = resolve(bundle, ref.replace(/^\/console\//, "").replace(/^\//, ""));
  if (!existsSync(target)) throw new Error(`bundle references missing asset: ${ref}`);
}
const scripts = assetRefs.filter((ref) => ref.endsWith(".js"));
if (!scripts.some((ref) => readFileSync(resolve(bundle, ref.replace(/^\/console\//, "").replace(/^\//, "")), "utf8").includes("Veyra"))) {
  throw new Error("generated JavaScript does not contain the Veyra shell");
}
const sourceRoot = resolve(root, "src");
const sourceFiles = [];
// Keep the gate dependency-free and deterministic: generated HTML/assets must
// be newer than the latest tracked source file. This catches a stale tracked
// ui/console bundle without rebuilding it inside CI.
const walkSync = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = resolve(dir, entry.name);
    if (entry.isDirectory()) walkSync(path);
    else if (/\.(tsx?|css)$/.test(entry.name)) sourceFiles.push(path);
  }
};
walkSync(sourceRoot);
const newestSource = Math.max(...sourceFiles.map((path) => statSync(path).mtimeMs));
const generatedFiles = [htmlPath, ...assetRefs.map((ref) => resolve(bundle, ref.replace(/^\/console\//, "").replace(/^\//, "")))];
const oldestGenerated = Math.min(...generatedFiles.map((path) => statSync(path).mtimeMs));
if (oldestGenerated + 1000 < newestSource) throw new Error("ui/console bundle is older than web/src; run npm run build");
console.log(`bundle freshness ok (${assetRefs.length} referenced assets)`);
