from probes.schema import probe_payload


class McpProbe:
    def run(self, text: str = "") -> dict:
        return probe_payload(
            probe="mcp_probe",
            target="mcp_runtime",
            status="unknown",
            summary="MCP probe is not configured.",
            confidence=0.35,
            ttl_seconds=120,
            details={"configured": False},
        )
