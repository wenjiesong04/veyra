from probes.schema import probe_payload


class NetworkProbe:
    def run(self, text: str = "") -> dict:
        return probe_payload(
            probe="network_probe",
            target="network",
            status="not_configured",
            summary="Network probe is not configured.",
            confidence=0.35,
            ttl_seconds=120,
            details={"configured": False},
        )
