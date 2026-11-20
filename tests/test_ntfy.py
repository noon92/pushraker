#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""pushraker.ntfy (ntfy with an access token, for Apprise 1.2.0) against a
fake ntfy server, and how a failure is worded.

  python3 tests/test_ntfy.py
"""

import base64
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import apprise  # noqa: E402
from pushraker import ntfy  # noqa: E402
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


GOT = []
TOKEN = "tk_TESTTESTTESTTESTTESTTESTTEST"


class Ntfy(BaseHTTPRequestHandler):
    """A protected topic: the token, or me/pw, or 403 with ntfy's code."""
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urllib.parse.urlsplit(self.path)
        GOT.append({"method": self.command, "path": u.path,
                    "q": dict(urllib.parse.parse_qsl(u.query)),
                    "auth": self.headers.get("Authorization", ""),
                    "body": body})
        ok = self.headers.get("Authorization") in (
            f"Bearer {TOKEN}",
            "Basic " + base64.b64encode(b"me:pw").decode())
        self.send_response(200 if ok else 403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": "x"} if ok else {
            "code": 40301, "http": 403, "error": "forbidden"}).encode())
    do_POST = do_PUT = _any

    def log_message(self, *a):
        pass


def run_ntfy_tests():
    print("pushraker.ntfy (ntfy with a token for Apprise 1.2.0)")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    s = dict(st.defaults("Hi"), provider="ntfy", server=f"http://{host}",
             topic="hi_x9", auth="token", secret=TOKEN,
             printer_url="http://192.168.0.220")
    old = st.Env(ntfy_token=False)
    url = st.row_url(s, "complete", "high", old)
    check("Apprise without ntfy tokens: pushraker's own sender",
          url.startswith("pntfy://") and "token=" in url, url)
    check("a newer Apprise: its own ntfy, as before",
          st.row_url(s, "complete", "high", st.Env()).startswith("ntfy://"))

    def send(u, attach=None):
        GOT.clear()
        a = apprise.Apprise()
        a.add(u)
        return bool(a.notify(title="⏳ Hi 50%", body="Benchy: 50 % · ½",
                             attach=attach))
    check("token: sent", send(url) is True, GOT)
    g = GOT[-1]
    check("...as a Bearer token, to the topic, with title and priority",
          g["auth"] == f"Bearer {TOKEN}" and g["path"] == "/hi_x9"
          and g["q"].get("title") == "⏳ Hi 50%"
          and g["q"].get("priority") == "high" and g["method"] == "POST"
          and g["body"].decode() == "Benchy: 50 % · ½", g)
    check("...with the buttons as ntfy actions",
          g["q"].get("actions", "").startswith("view, Web UI, "
                                               "http://192.168.0.220/"), g)
    pic = os.path.join(tempfile.mkdtemp(), "snapshot.jpg")
    with open(pic, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\0" * 500)
    check("with a picture: sent", send(url, attach=pic) is True)
    g = GOT[-1]
    check("...PUT with the picture, the text as a parameter",
          g["method"] == "PUT" and g["body"].startswith(b"\xff\xd8")
          and g["q"].get("message") == "Benchy: 50 % · ½"
          and g["q"].get("filename") == "snapshot.jpg", g["q"])
    basic = st.row_url(dict(s, auth="basic", username="me", secret="pw"),
                       "complete", "high", old)
    check("user and password: sent", send(basic) is True
          and GOT[-1]["auth"].startswith("Basic "), GOT[-1:])
    bad = st.row_url(dict(s, secret="tk_WRONG"), "complete", "high", old)
    check("a wrong token: not sent", send(bad) is False)
    https = st.row_url(dict(s, server="https://ntfy.sh"), "complete",
                       "high", old)
    u = urllib.parse.urlsplit(https)
    req = ntfy.request({"host": u.hostname, "port": u.port,
                        "fullpath": u.path,
                        "qsd": dict(urllib.parse.parse_qsl(u.query))},
                       "b", "t", None)
    check("ntfy.sh: https, the token too (Apprise 1.2.0 dropped any auth "
          "for ntfy.sh)", req["url"] == "https://ntfy.sh/hi_x9"
          and req["headers"].get("Authorization") == f"Bearer {TOKEN}", req)
    srv.shutdown()

    # How a refusal is worded in pushraker's console line
    import pushraker.component as pn
    check("ntfy's 5-digit code: '403 Forbidden', not 'failed'",
          pn.short_cause(["Failed to send ntfy notification to topic 'x': "
                          "forbidden, error=40301."]) == "403 Forbidden")
    log = pn.AppriseLog(100)
    import logging
    rec = logging.LogRecord(
        "apprise", logging.WARNING,
        "/usr/share/moonraker-env/lib/python3.9/site-packages/apprise/"
        "plugins/NotifyNtfy.py", 1, "forbidden, error=40301.", None, None)
    log.emit(rec)
    check("Apprise 1.2.0's NotifyNtfy.py lines are ntfy's",
          log.take("ntfy") == ["forbidden, error=40301."])
    return _failures


if __name__ == "__main__":
    run_ntfy_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
