#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Whether pushraker works with the Apprise, Pillow and Jinja2 of the
Python it runs with: every service's request is built by Apprise as for a
real send, but requests and smtplib are replaced by recorders, so nothing
leaves the machine. Run it with the Python Moonraker runs with, e.g. on a
printer with old packages:

  /usr/share/moonraker-env/bin/python check_deps.py [<dir holding pushraker>]

Exits non-zero if anything fails.
"""

import asyncio
import json
import os
import sys
import tempfile

if len(sys.argv) > 1:
    # At the end, as the component file does: the printer's own packages win
    sys.path.append(sys.argv[1])
else:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src"))

import apprise  # noqa: E402
import jinja2  # noqa: E402
import PIL  # noqa: E402
import requests  # noqa: E402
import smtplib  # noqa: E402

failures = 0


def check(name, ok, detail=""):
    global failures
    print(("  ok    " if ok else "  FAIL  ") + name
          + ("" if ok or not detail else f": {detail}"))
    if not ok:
        failures += 1


print(f"python {sys.version.split()[0]}, apprise {apprise.__version__}, "
      f"Pillow {PIL.__version__}, Jinja2 {jinja2.__version__}, "
      f"requests {requests.__version__}")

# ---- recorders instead of the network --------------------------------------

SENT = []


class FakeResponse:
    status_code = 200
    ok = True
    reason = "OK"
    headers = {"Content-Type": "application/json"}

    def __init__(self, url):
        self.url = url
        # Telegram wants ok/result; Discord a message or channel id
        self.content = json.dumps({"ok": True, "result": {"message_id": 1},
                                   "id": "1"}).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_request(method):
    def call(url, *a, **kw):
        files = kw.get("files")
        data = kw.get("data")
        if hasattr(data, "read"):
            data = b"<file>"
        SENT.append(dict(method=method.upper(), url=str(url),
                         files=bool(files) or isinstance(kw.get("data"),
                                                          (bytes,))
                         or hasattr(kw.get("data"), "read"),
                         body=str(data)[-600:] if data else
                         str(kw.get("json"))[-600:],
                         # Everything a server reads: what the checks
                         # below look for (auth, target, priority)
                         all=" ".join(str(x) for x in (
                             url, kw.get("params"), kw.get("headers"),
                             kw.get("auth"), data, kw.get("json")))))
        return FakeResponse(url)
    return call


for m in ("get", "post", "put", "patch", "delete"):
    setattr(requests, m, fake_request(m))
requests.request = lambda method, url, *a, **kw: fake_request(method)(
    url, *a, **kw)
requests.Session.request = lambda self, method, url, *a, **kw: \
    fake_request(method)(url, *a, **kw)


class FakeSMTP:
    def __init__(self, host="", port=0, *a, **kw):
        self.host, self.port = host, port

    def ehlo(self, *a):
        return (250, b"ok")

    def starttls(self, *a, **kw):
        return (220, b"ok")

    def login(self, *a):
        SENT.append(dict(method="LOGIN", url=f"{self.host}:{self.port}",
                         files=False, body="", all="login " + " ".join(a)))
        return (235, b"ok")

    def sendmail(self, frm, to, msg, *a, **kw):
        SENT.append(dict(method="SMTP", url=f"{self.host}:{self.port}",
                         files="Content-Disposition" in str(msg)
                         or "image/jpeg" in str(msg),
                         body=str(msg)[:400],
                         all=f"from {frm} to {to}"))
        return {}

    send_message = sendmail

    def quit(self):
        pass

    close = quit

    def set_debuglevel(self, *a):
        pass


smtplib.SMTP = FakeSMTP
smtplib.SMTP_SSL = FakeSMTP

# ---- pushraker -----------------------------------------------------------

from pushraker import settings as cfg  # noqa: E402
from pushraker import discord as _discord  # noqa: E402,F401  (registers it)
from pushraker import email_html, engine, images  # noqa: E402

work = tempfile.mkdtemp(prefix="pushraker_deps_")
jpg = images.problem("No camera picture", "check_deps.py test",
                     os.path.join(work, "p.jpg"))
check("images.problem draws a JPEG", os.path.getsize(jpg) > 1000)
rows = [dict(toolhead="T1", word="", status="green", needs="12 g",
             remaining=640., full=1000., colour=(48, 128, 208),
             name="PLA Blue"),
        dict(toolhead="T2", word="", status="red", needs="",
             remaining=0., full=1000., colour=(224, 224, 224), name="PETG",
             refills=[dict(toolhead="T3", remaining=300., full=1000.,
                           colour=(255, 136, 0), name="PLA Orange",
                           status="green", match="Exact match")])]
try:
    out = images.render(rows, os.path.join(work, "spools.jpg"))
    check("images.render draws the spool picture",
          os.path.getsize(out) > 1000)
except Exception as e:
    check("images.render draws the spool picture", False, repr(e))
f = images.font(True, 30)
check("a font at a size (TrueType, or Pillow's own)", f is not None,
      type(f).__name__)

html = email_html.render("Benchy complete", "**Done** in 1 h", [
    ("Web UI", "http://printer/")], "success", "p.jpg")
check("email HTML card", "<html" in html.lower() or "<table" in html.lower())

try:
    eng = engine.Engine() if hasattr(engine, "Engine") else None
    check("the Jinja engine loads", True)
except Exception as e:
    check("the Jinja engine loads", False, repr(e))

s = cfg.defaults("Hi")
# As the component builds it: with an Apprise whose ntfy can't take a
# token, pushraker's own ntfy sender
from pushraker import ntfy as _ntfy  # noqa: E402
env = cfg.Env(ntfy_token=_ntfy.apprise_has_token())
print(f"Apprise's own ntfy takes a token: {env.ntfy_token}")
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123"
services = {
    "ntfy": dict(provider="ntfy", server="https://ntfy.sh", topic="hi_test_x9",
                 auth="token", secret="tk_" + TOKEN),
    "ntfy (basic auth, own server)": dict(
        provider="ntfy", server="http://192.168.0.5:8080", topic="hi",
        auth="basic", username="me", secret="pw"),
    "pushover": dict(provider="pushover", pushover_user="u" + TOKEN[:29],
                     secret="a" + TOKEN[:29], pushover_device=""),
    "gotify": dict(provider="gotify", gotify_server="https://gotify.example.com",
                   secret="A" + TOKEN[:14]),
    "telegram": dict(provider="telegram", secret="123456789:" + TOKEN + "abcd12",
                     telegram_chat="12345678"),
    "discord (webhook)": dict(
        provider="discord", discord_mention="",
        secret="https://discord.com/api/webhooks/123456789012345678/"
               + TOKEN + TOKEN),
    "discord (DM)": dict(provider="discord_dm", secret="MTIz." + TOKEN,
                         discord_user="123456789012345678"),
}


async def send(url, attach=True, body="Body *text*", fmt=None):
    ap = apprise.Apprise()
    if not ap.add(url):
        return None, "Apprise rejected the URL"
    n = len(SENT)
    kw = {}
    if fmt:
        kw["body_format"] = fmt
    ok = await ap.async_notify(title="Benchy complete", body=body,
                               attach=jpg if attach else None, **kw)
    return ok, SENT[n:]


# What each server needs to see, somewhere in the requests (credentials,
# where to, how urgent): a request that is merely built isn't enough
NEEDS = {
    "ntfy": ["Bearer tk_" + TOKEN, "hi_test_x9", "ntfy.sh"],
    "ntfy (basic auth, own server)": ["192.168.0.5", "/hi",
                                      "('me', 'pw')|Basic bWU6cHc="],
    "pushover": ["u" + TOKEN[:29], "a" + TOKEN[:29]],
    "gotify": ["A" + TOKEN[:14], "gotify.example.com"],
    "telegram": ["123456789:" + TOKEN + "abcd12", "12345678"],
    "discord (webhook)": ["123456789012345678", TOKEN + TOKEN],
    "discord (DM)": ["Bot MTIz." + TOKEN, "123456789012345678"],
}


def has(sent, needle):
    return any(any(n in x.get("all", "") for n in needle.split("|"))
               for x in sent)


async def main():
    for name, fields in services.items():
        st = dict(s, **fields)
        url = cfg.row_url(st, "complete", "high", env)
        ok, sent = await send(url)
        check(f"{name}: request built", ok and bool(sent),
              sent if isinstance(sent, str) else f"ok={ok} sent={sent}")
        if ok and sent:
            missing = [n for n in NEEDS[name] if not has(sent, n)]
            check(f"{name}: carries what the server needs "
                  "(credentials, target)", not missing,
                  f"missing {missing}")
            if fields["provider"] == "ntfy":
                check(f"{name}: priority high", has(
                    sent, "'priority': 'high'|'X-Priority': '4'|'X-Priority': 'high'|"
                          "priority=high|'Priority': '4'"),
                      [x["all"][:300] for x in sent])
        if ok and sent and fields["provider"] != "gotify":
            check(f"{name}: picture attached", any(x["files"] for x in sent),
                  [(x["method"], x["url"][:60]) for x in sent])
    st = dict(s, webhook_enabled=True,
              webhook_url="https://example.com/hook?key=abc")
    ok, sent = await send(cfg.webhook_url(st, "complete", env))
    check("webhook (JSON): request built", ok and bool(sent), sent)
    if ok and sent:
        body = sent[-1]["body"]
        check("webhook: event and printer fields", "complete" in body
              and "Hi" in body, body[:200])
    for sec, port in (("starttls", 587), ("ssl", 465), ("none", 25)):
        st = dict(s, email_enabled=True, email_host="smtp.example.com",
                  email_port=port, email_security=sec, email_username="me",
                  email_password="pw", email_from="me@example.com",
                  email_to="you@example.com")
        try:
            url = cfg.email_url(st)
        except Exception as e:
            check(f"email ({sec}): URL", False, repr(e))
            continue
        ok, sent = await send(url, body=html, fmt="html")
        check(f"email ({sec}): message built", ok and bool(sent),
              sent if isinstance(sent, str) else f"ok={ok}")
        if ok and sent:
            check(f"email ({sec}): logs in, from and to",
                  has(sent, "login me pw") and has(sent, "me@example.com")
                  and has(sent, "you@example.com"),
                  [x.get("all") for x in sent])

asyncio.run(main())
print(f"\n{'all ok' if not failures else f'{failures} failed'}")
sys.exit(1 if failures else 0)
