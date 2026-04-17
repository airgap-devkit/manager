"""
Connectivity detection — determines whether the machine can reach the internet.
"""
from __future__ import annotations

import socket
from typing import Literal

_PROBE_HOSTS = ["8.8.8.8", "1.1.1.1"]
_PROBE_PORT = 443
_PROBE_TIMEOUT = 2  # seconds per host


def detect_mode() -> Literal["online", "airgapped"]:
    """Return 'online' if any probe host is reachable on port 443, else 'airgapped'."""
    for host in _PROBE_HOSTS:
        try:
            with socket.create_connection((host, _PROBE_PORT), timeout=_PROBE_TIMEOUT):
                return "online"
        except OSError:
            continue
    return "airgapped"
