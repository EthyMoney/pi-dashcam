"""Home detection.

The authoritative "am I home?" test is a TCP connect to the NAS: it proves the
exact path an upload needs (association, DHCP, routing, NAS up). Seeing the
home SSID in a scan proves none of that, so the SSID is only reported for
logs and the display.
"""

import logging
import os
import shutil
import socket
import subprocess
from typing import Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def host_port_from_url(url: str) -> Tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def current_ssid() -> Optional[str]:
    """SSID of the associated network, or None. Uses iwgetid, then nmcli."""
    env = {**os.environ, "LC_ALL": "C"}   # nmcli localises "yes"/"no" even in terse mode
    if shutil.which("iwgetid"):
        try:
            out = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5, env=env)
            ssid = out.stdout.strip()
            if ssid:
                return ssid
        except Exception as exc:
            log.debug("iwgetid failed: %s", exc)
    if shutil.which("nmcli"):
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                                 capture_output=True, text=True, timeout=10, env=env)
            for line in out.stdout.splitlines():
                if line.startswith("yes:"):
                    return line[4:].replace("\\:", ":").strip() or None
        except Exception as exc:
            log.debug("nmcli failed: %s", exc)
    return None


class HomeDetector:
    def __init__(self, nas_url: str, timeout: float = 3.0, home_ssid: str = ""):
        self.host, self.port = host_port_from_url(nas_url) if nas_url else ("", 0)
        self.timeout = timeout
        self.home_ssid = home_ssid

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def nas_reachable(self) -> bool:
        return tcp_reachable(self.host, self.port, self.timeout)

    def describe(self) -> str:
        ssid = current_ssid()
        reach = self.nas_reachable() if self.configured else False
        parts = [f"wifi={ssid or 'none'}"]
        if self.home_ssid:
            parts.append("home-ssid" if ssid == self.home_ssid else "not-home-ssid")
        parts.append(f"nas={'reachable' if reach else 'unreachable'}")
        return " ".join(parts)
