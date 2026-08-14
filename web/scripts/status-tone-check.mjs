const bad = ["unavailable", "unhealthy", "disconnected", "not_configured", "fail_closed"];
const good = ["available", "connected", "configured", "healthy", "ready"];
const tone = (value) => {
  const normalized = String(value).toLowerCase();
  if (["error", "failed", "blocked", "denied", ...bad].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "bad";
  if (["degraded", "stale", "loading", "pending", "unknown", "waiting"].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "warn";
  if (["ready", "active", ...good, "success", "passed", "fresh"].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "good";
  return "neutral";
};
for (const value of bad) if (tone(value) !== "bad") throw new Error(`${value} was not bad`);
for (const value of good) if (tone(value) !== "good") throw new Error(`${value} was not good`);
console.log("status tone checks passed");
