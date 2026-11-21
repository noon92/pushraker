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
import os
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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


# The printer's web pages, for the page's Printer URL "Fill in" when the
# page is served by Moonraker itself (its port is the API's, wrong for the
# Web UI button): the ports web UIs usually listen on (Fluidd and Mainsail
# on 80; KIAUH's and Creality's second UIs on 4408/4409; 81, 8080, 8888
# seen elsewhere) and every `listen` in nginx's configuration. Only ports
# that are listening are asked, with one GET of "/" that must answer
# 200 with HTML. (The Creality Hi: Fluidd on 4408; its port 80 is
# Creality's API, 404 at "/".)
WEB_PORTS = (80, 81, 4408, 4409, 8080, 8888)
NGINX_CONFS = ("/etc/nginx/nginx.conf", "/etc/nginx/conf.d",
               "/etc/nginx/sites-enabled")
RX_LISTEN = re.compile(r"(?<![\w-])listen\s+(?:[\w.:\[\]]*:)?(\d+)\b")
RX_TITLE = re.compile(rb"<title[^>]*>([^<]*)", re.I)
KNOWN_UIS = (("fluidd", "Fluidd"), ("mainsail", "Mainsail"),
             ("octoprint", "OctoPrint"))
PAGE_BYTES = 65536


def nginx_ports(paths: Sequence[str] = NGINX_CONFS) -> List[int]:
    """The ports nginx's configuration listens on."""
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            try:
                files += [os.path.join(p, n) for n in sorted(os.listdir(p))]
            except OSError:
                pass
        elif os.path.isfile(p):
            files.append(p)
    out: List[int] = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read(1 << 20)
        except OSError:
            continue
        for m in RX_LISTEN.finditer(re.sub(r"#[^\n]*", "", text)):
            port = int(m.group(1))
            if 0 < port < 65536 and port not in out:
                out.append(port)
    return out


def page_title(port: int, timeout: float = GET_TIMEOUT) -> Optional[str]:
    """The title of the HTML page at "/" on this port ("" untitled), or
    None when it isn't one."""
    url = f"http://127.0.0.1:{port}/"
    if not port_open(url):
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200 or "html" not in \
                    (r.headers.get("Content-Type") or "").lower():
                return None
            data = r.read(PAGE_BYTES)
    except (OSError, ValueError):
        return None
    m = RX_TITLE.search(data)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def page_name(title: str) -> str:
    low = title.lower()
    for key, name in KNOWN_UIS:
        if key in low:
            return name
    title = " ".join(title.split())
    return title[:40] if title else "Web page"


def web_pages(skip: Iterable[int] = (), ports: Sequence[int] = WEB_PORTS,
              nginx: Sequence[str] = NGINX_CONFS,
              title=page_title) -> List[Dict[str, Any]]:
    """[{"port", "name"}] of the web pages on this machine, the known web
    UIs first: never a port in `skip` (Moonraker's own, the cameras'),
    never Moonraker's own page."""
    skip = set(skip)
    found: List[Dict[str, Any]] = []
    for port in list(ports) + [p for p in nginx_ports(nginx)
                               if p not in ports]:
        if port in skip:
            continue
        t = title(port)
        if t is None or "moonraker" in t.lower():
            continue
        found.append({"port": port, "name": page_name(t)})
    known = [n for _, n in KNOWN_UIS]
    found.sort(key=lambda p: (p["name"] not in known, p["port"]))
    return found


__all__: List[str] = ["discover", "probe", "CANDIDATES", "web_pages"]
