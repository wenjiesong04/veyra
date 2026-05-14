from probes.port_probe import PortProbe


class OpenClawProbe:
    def run(self, text: str = "") -> dict:
        result = PortProbe().run(text or "18789")
        result["probe"] = "openclaw_probe"
        result["runtime"] = "openclaw"
        return result
