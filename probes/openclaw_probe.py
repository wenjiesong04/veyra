from probes.port_probe import PortProbe


class OpenClawProbe:
    def run(self, text: str = "") -> dict:
        result = PortProbe().run(text or "18789")
        result["probe"] = "openclaw_probe"
        result["source"] = "openclaw_probe"
        result["target"] = "openclaw_runtime"
        result["runtime"] = "openclaw"
        result["summary"] = f"OpenClaw runtime port {result.get('port')} is {result.get('status')}."
        return result
