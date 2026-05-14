import re
import socket

from interface.event_schema import utc_now_iso


class PortProbe:
    def run(self, text: str = "") -> dict:
        port = self._extract_port(text) or 18789
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            listening = sock.connect_ex(("127.0.0.1", port)) == 0
        status = "listening" if listening else "closed"
        return {
            "probe": "port_probe",
            "port": port,
            "host": "127.0.0.1",
            "status": status,
            "timestamp": utc_now_iso(),
            "summary": f"Port {port} is {status}.",
        }

    def _extract_port(self, text: str) -> int | None:
        match = re.search(r"\b([1-9][0-9]{1,4})\b", text)
        if not match:
            return None
        port = int(match.group(1))
        return port if 0 < port <= 65535 else None
