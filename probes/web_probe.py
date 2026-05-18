from probes.schema import probe_payload


class WebProbe:
    def run(self, text: str = "") -> dict:
        return probe_payload(
            probe="web_probe",
            target="web",
            status="not_configured",
            summary="Web probe is not configured.",
            confidence=0.35,
            ttl_seconds=120,
            details={"configured": False},
        )
