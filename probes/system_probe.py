import platform

from interface.event_schema import utc_now_iso


class SystemProbe:
    def run(self, text: str = "") -> dict:
        return {
            "probe": "system_probe",
            "status": "ok",
            "os": platform.system(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "timestamp": utc_now_iso(),
            "summary": f"System is {platform.system()} with Python {platform.python_version()}.",
        }
