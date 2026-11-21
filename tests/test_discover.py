#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""pushraker.discover against a fake local camera server: what counts as
a camera, what doesn't, and what is left to Moonraker.

  python3 tests/test_discover.py
"""

import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from pushraker import discover as d  # noqa: E402
from pushraker import settings as st  # noqa: E402

_passes = 0
_failures = 0


def check(name, ok, detail=""):
    global _passes, _failures
    if ok:
        _passes += 1
        print(f"  ok    {name}")
    else:
        _failures += 1
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 2000
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000


class Cam(BaseHTTPRequestHandler):
    def do_GET(self):
        body, kind = {
            "/jpeg": (JPEG, "image/jpeg"),
            "/png": (PNG, "image/png"),
            "/html": (b"<!DOCTYPE html><html>Video On Demand</html>",
                      "text/html"),
            "/liar": (b"<html>not an image</html>", "image/jpeg"),
            "/big": (JPEG + b"\0" * 5000, "image/jpeg"),
        }.get(self.path, (None, None))
        if self.path == "/slow":
            time.sleep(1.5)
            body, kind = JPEG, "image/jpeg"
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def run_discover_tests():
    print("pushraker.discover")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Cam)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = f"http://127.0.0.1:{s.getsockname()[1]}/?action=snapshot"
    s.close()

    def probe(path, **kw):
        return d.probe(base + path, **kw)
    check("a JPEG is a camera", probe("/jpeg"))
    check("a PNG is a camera", probe("/png"))
    check("an HTML page is not (the Hi's WebRTC page on :8000)",
          not probe("/html"))
    check("an 'image/jpeg' that isn't one is not", not probe("/liar"))
    check("a 404 is not", not probe("/nothing"))
    t = time.monotonic()
    check("too slow: given up", not probe("/slow", timeout=0.5))
    check("...within the timeout", time.monotonic() - t < 1.4)
    check("too big: not taken", not probe("/big", max_bytes=3000)
          and probe("/jpeg", max_bytes=3000))
    t = time.monotonic()
    check("nothing listening: refused at once", not d.probe(closed)
          and time.monotonic() - t < 1.0)

    cands = (("Camera (port A)", base + "/html", ":1/a"),
             ("Camera (port A)", base + "/jpeg", ":1/stream"),
             ("Camera (port B)", closed, ":2/x"),
             ("Camera (port C)", base + "/png", ":3/y"))
    found = d.discover((), cands)
    check("the second address of a port is tried when the first isn't a "
          "camera; one camera per name",
          found == {"Camera (port A)": {"snapshot": base + "/jpeg",
                                        "stream": ":1/stream"},
                    "Camera (port C)": {"snapshot": base + "/png",
                                        "stream": ":3/y"}}, found)
    known = [base.replace("127.0.0.1", "localhost") + "/png"]
    found = d.discover(known, cands)
    check("an address Moonraker's webcams already have is left out",
          list(found) == ["Camera (port A)"], found)
    calls = []
    found = d.discover((), d.CANDIDATES,
                       check=lambda u: calls.append(u) or ":8080/?" in u)
    check("nginx's /webcam/ is not probed when port 8080 answered (the same "
          "camera)", "Camera (/webcam/)" not in found
          and not any("/webcam/" in u for u in calls)
          and "Camera (port 8080)" in found, (found, calls))
    check("same_url: localhost and 127.0.0.1, default port",
          d.same_url("http://localhost/webcam/?action=snapshot",
                     "http://127.0.0.1:80/webcam/?action=snapshot")
          and not d.same_url("http://127.0.0.1:8080/?action=snapshot",
                             "http://127.0.0.1:8081/?action=snapshot"))
    srv.shutdown()

    # The Live Camera link of a camera on its own port
    s0 = st.defaults("Hi")
    env = st.Env(cameras={"Camera (port 8080)": {
        "snapshot": "http://127.0.0.1:8080/?action=snapshot",
        "stream": ":8080/?action=stream"}})
    s1 = dict(s0, camera="Camera (port 8080)",
              printer_url="http://192.168.0.220:4408")
    check("Live Camera: that port on the Printer URL's host",
          st.camera_live_url(s1, env)
          == "http://192.168.0.220:8080/?action=stream",
          st.camera_live_url(s1, env))
    s2 = dict(s1, printer_url="https://printer.example.com")
    check("...also behind a name and https",
          st.camera_live_url(s2, env)
          == "https://printer.example.com:8080/?action=stream")
    run_web_page_tests()
    return _failures


def run_web_page_tests():
    """The printer's web pages, for the page's Printer URL "Fill in"."""
    import tempfile

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            body, kind, code = PAGES.get(self.server.server_address[1],
                                         (b"", "text/html", 404))
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Page) for _ in range(4)]
    ports = [srv.server_address[1] for srv in servers]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    PAGES = {
        ports[0]: (b"<html><head><title>fluidd</title></head></html>",
                   "text/html", 200),
        ports[1]: (b'{"error": "not found"}', "text/html", 404),
        ports[2]: (b"<title>Moonraker</title>", "text/html", 200),
        ports[3]: (b"<HTML><TITLE> Video\n On Demand </TITLE>",
                   "text/html; charset=utf-8", 200)}
    check("page_title: a page's title, none for a 404",
          d.page_title(ports[0]) == "fluidd"
          and d.page_title(ports[1]) is None)
    conf = tempfile.mkdtemp()
    with open(os.path.join(conf, "nginx.conf"), "w") as f:
        f.write(f"server {{\n    listen {ports[3]} default_server;\n"
                f"    listen [::]:{ports[3]};\n}}\n"
                f"server {{ listen 127.0.0.1:{ports[1]}; }}\n"
                "# listen 1;\n")
    check("nginx_ports: every listen, once, comments aside",
          d.nginx_ports([conf]) == [ports[3], ports[1]],
          d.nginx_ports([conf]))
    found = d.web_pages(skip=[], ports=ports[:3], nginx=[conf])
    check("web_pages: Fluidd first, a titled page by its title; no 404, "
          "never Moonraker's own page",
          found == [{"port": ports[0], "name": "Fluidd"},
                    {"port": ports[3], "name": "Video On Demand"}], found)
    found = d.web_pages(skip=[ports[3]], ports=ports[:3], nginx=[conf])
    check("...a port to skip (Moonraker's, a camera's) is never asked",
          found == [{"port": ports[0], "name": "Fluidd"}], found)
    check("page_name: the known UIs by name",
          d.page_name("Mainsail") == "Mainsail" and d.page_name("") ==
          "Web page")
    for srv in servers:
        srv.shutdown()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    closed = free.getsockname()[1]
    free.close()
    check("a port nothing listens on: no page", d.page_title(closed) is None)


if __name__ == "__main__":
    run_discover_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
