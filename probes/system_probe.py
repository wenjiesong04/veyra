import os
import platform
import shutil

from probes.schema import probe_payload

# Percent thresholds above which a resource is considered under pressure.
_PRESSURE_THRESHOLD = 90.0


class SystemProbe:
    def run(self, text: str = "") -> dict:
        resources = self._resources()
        pressure = self._pressure(resources)
        details = {
            "os": platform.system(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "resources": resources,
            "resource_pressure": pressure,
        }
        summary = f"System is {platform.system()} with Python {platform.python_version()}."
        if pressure:
            summary += " Resource pressure: " + ", ".join(pressure) + "."
        return probe_payload(
            probe="system_probe",
            target="local_system",
            status="resource_pressure" if pressure else "ok",
            summary=summary,
            confidence=0.95,
            ttl_seconds=300,
            details=details,
        )

    def _resources(self) -> dict:
        resources: dict[str, float] = {}
        try:
            usage = shutil.disk_usage("/")
            if usage.total:
                resources["disk_percent"] = round(usage.used / usage.total * 100, 1)
                resources["disk_free_gb"] = round(usage.free / (1024**3), 2)
        except Exception:
            pass
        try:
            if hasattr(os, "getloadavg"):
                load1 = os.getloadavg()[0]
                cores = os.cpu_count() or 1
                resources["cpu_load1"] = round(load1, 2)
                resources["cpu_load_percent"] = round(load1 / cores * 100, 1)
        except Exception:
            pass
        memory = self._memory_percent()
        if memory is not None:
            resources["memory_percent"] = memory
        return resources

    def _memory_percent(self) -> float | None:
        try:
            import psutil  # type: ignore

            return round(float(psutil.virtual_memory().percent), 1)
        except Exception:
            pass
        try:
            data: dict[str, str] = {}
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    key, _, value = line.partition(":")
                    data[key.strip()] = value
            total = int(data["MemTotal"].split()[0])
            available = int(data["MemAvailable"].split()[0])
            if total:
                return round((total - available) / total * 100, 1)
        except Exception:
            pass
        return None

    def _pressure(self, resources: dict) -> list[str]:
        pressure: list[str] = []
        for key, label in (("disk_percent", "disk"), ("memory_percent", "memory"), ("cpu_load_percent", "cpu")):
            value = resources.get(key)
            if isinstance(value, (int, float)) and value >= _PRESSURE_THRESHOLD:
                pressure.append(f"{label}_{value}%")
        return pressure
