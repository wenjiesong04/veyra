from __future__ import annotations

import os

from probes.port_probe import PortProbe


class OpenClawProbe:
    def run(self, text: str = "") -> dict:
        configured = bool(text.strip() or os.getenv("OPENCLAW_BASE_URL") or os.getenv("OPENCLAW_GATEWAY_URL"))
        result = PortProbe().run(text or "18789")
        result["probe"] = "openclaw_probe"
        result["source"] = "openclaw_probe"
        result["target"] = "openclaw_runtime"
        result["runtime"] = "openclaw"
        result["summary"] = f"OpenClaw runtime port {result.get('port')} is {result.get('status')}."
        result["details"] = {
            **(result.get("details") if isinstance(result.get("details"), dict) else {}),
            "configured": configured,
            "validation_note": "Port probe only; /agent/status performs Gateway protocol validation.",
        }
        result["validation"] = {
            "source": "real_probe",
            "observed": True,
            "configured": configured,
            "validated": bool(configured and result.get("status") == "listening"),
            "status": "validated" if configured and result.get("status") == "listening" else "validation_pending" if configured else "not_configured",
        }
        return result
