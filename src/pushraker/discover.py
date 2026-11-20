# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Cameras Moonraker doesn't know: many printers run a camera server with
no [webcam] in moonraker.conf (Sovol SV08, QIDI Q1 Pro, Creality K1,
Flashforge AD5M Klipper Mod, a set-up Elegoo Neptune 4; see
findings-rev3.md in the multi-printer work). Probed once per Klippy
ready, on this machine only, at the snapshot addresses the common camera
servers answer on (mjpg-streamer, ustreamer/crowsnest, camera-streamer):
a quick connect first, then one short GET that must return a real JPEG or
PNG. go2rtc is left out: it needs a stream name and decodes per request.
Blocking: run it in a thread.
"""

from __future__ import annotations
import socket
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Sequence, Tuple

CONNECT_TIMEOUT = 0.3
GET_TIMEOUT = 2.0
MAX_BYTES = 5 * 1024 * 1024
JPEG = b"\xff\xd8\xff"
PNG = b"\x89PNG\r\n\x1a\n"

# (name, snapshot URL, stream for the notification's Live Camera link): a
# stream ":<port>/path" is that port on the Printer URL's host. One camera
# per name: the second 8080 address is only tried when the first didn't
# answer (camera-streamer/ustreamer without the ?action= layer), and
# nginx's /webcam/ is crowsnest's and KIAUH's proxy for port 8080, so it
# is only tried without a camera on 8080.
CANDIDATES: Tuple[Tuple[str, str, str], ...] = (
    ("Camera (port 8080)", "http://127.0.0.1:8080/?action=snapshot",
     ":8080/?action=stream"),
    ("Camera (port 8080)", "http://127.0.0.1:8080/snapshot",
     ":8080/stream"),
    ("Camera (port 8081)", "http://127.0.0.1:8081/?action=snapshot",
     ":8081/?action=stream"),
    ("Camera (/webcam/)", "http://127.0.0.1/webcam/?action=snapshot",
     "/webcam/?action=stream"),
)
SAME_AS = {"Camera (/webcam/)": "Camera (port 8080)"}


def same_url(a: str, b: str) -> bool:
    """The same address, localhost or 127.0.0.1, default port or not."""
    def key(u: str) -> Tuple[str, int, str]:
        p = urllib.parse.urlsplit(u)
        host = (p.hostname or "").lower()
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        port = p.port or (443 if p.scheme == "https" else 80)
        return host, port, (p.path or "/") + ("?" + p.query if p.query
                                               else "")
    try:
        return key(a) == key(b)
    except ValueError:
        return False


def is_image(data: bytes) -> bool:
    return data.startswith(JPEG) or data.startswith(PNG)


def port_open(url: str, timeout: float = CONNECT_TIMEOUT) -> bool:
    p = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((p.hostname or "127.0.0.1",
                                       p.port or 80), timeout):
            return True
    except OSError:
        return False


def probe(url: str, timeout: float = GET_TIMEOUT,
          max_bytes: int = MAX_BYTES) -> bool:
    """Whether the address returns one real JPEG or PNG, quickly."""
    if not port_open(url):
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return False
            data = r.read(max_bytes + 1)
    except (OSError, ValueError):
        return False
    return len(data) <= max_bytes and is_image(data)


def discover(known: Iterable[str] = (),
             candidates: Sequence[Tuple[str, str, str]] = CANDIDATES,
             check=probe) -> Dict[str, Dict[str, str]]:
    """{name: {"snapshot", "stream"}} of the cameras found, leaving out
    any address in `known` (Moonraker's webcams already have it)."""
    known = list(known)
    found: Dict[str, Dict[str, str]] = {}
    for name, url, stream in candidates:
        if name in found or SAME_AS.get(name) in found:
            continue
        if any(same_url(url, k) for k in known if k):
            continue
        if check(url):
            found[name] = {"snapshot": url, "stream": stream}
    return found


__all__: List[str] = ["discover", "probe", "CANDIDATES"]
