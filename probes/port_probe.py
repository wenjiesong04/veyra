import re
import socket

from probes.schema import probe_payload


class PortProbe:
    def run(self, text: str = "") -> dict:
        port = self._extract_port(text) or 18789
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            listening = sock.connect_ex(("127.0.0.1", port)) == 0
        status = "listening" if listening else "closed"
        return probe_payload(
            probe="port_probe",
            target=f"127.0.0.1:{port}",
            status=status,
            summary=f"Port {port} is {status}.",
            confidence=0.92,
            ttl_seconds=30,
            details={"port": port, "host": "127.0.0.1"},
        )

    def _extract_port(self, text: str) -> int | None:
        # Prefer an explicit host:port target before looking for a standalone
        # number. Otherwise ``127.0.0.1:8000`` is incorrectly interpreted as
        # port 127, the first numeric token in the IPv4 address.
        host_port = re.search(
            r"(?:\[[0-9A-Fa-f:.]+\]|localhost|(?:[0-9]{1,3}\.){3}[0-9]{1,3}|[A-Za-z][A-Za-z0-9.-]*):([0-9]{1,5})(?![0-9])",
            text,
            flags=re.IGNORECASE,
        )
        if host_port:
            return self._valid_port(host_port.group(1))

        labelled = re.search(
            r"(?:\bport\b|端口)\s*(?:[:=#]|is|是|为)?\s*([0-9]{1,5})(?![0-9])",
            text,
            flags=re.IGNORECASE,
        )
        if labelled:
            return self._valid_port(labelled.group(1))

        # Do not treat an IPv4 octet as a standalone port when an address is
        # present without an explicit port.
        standalone = re.search(r"(?<![0-9.:])([1-9][0-9]{1,4})(?![0-9.:])", text)
        return self._valid_port(standalone.group(1)) if standalone else None

    def _valid_port(self, value: str) -> int | None:
        port = int(value)
        return port if 0 < port <= 65535 else None
