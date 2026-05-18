from probes.schema import probe_payload


class HermesProbe:
    def run(self, text: str = "") -> dict:
        return probe_payload(
            probe="hermes_probe",
            target="hermes_runtime",
            status="unknown",
            summary="Hermes probe is not configured.",
            confidence=0.35,
            ttl_seconds=120,
            details={"configured": False},
        )
