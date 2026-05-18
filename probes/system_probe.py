import platform

from probes.schema import probe_payload


class SystemProbe:
    def run(self, text: str = "") -> dict:
        return probe_payload(
            probe="system_probe",
            target="local_system",
            status="ok",
            summary=f"System is {platform.system()} with Python {platform.python_version()}.",
            confidence=0.98,
            ttl_seconds=3600,
            details={
                "os": platform.system(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
        )
