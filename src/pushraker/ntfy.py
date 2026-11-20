# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

# ntfy for an Apprise without access tokens. Apprise 1.2.0 (the Creality
# Hi's) ignores ntfy's ?token= and posts anonymously, so a protected topic
# answers 403; it also drops user and password for ntfy.sh. Where the
# installed Apprise can't take a token, settings.row_url gives this sender
# instead (an Apprise custom plugin, like the Discord ones: the same
# queues, retries and fallbacks):
#
#   pntfy://<host[:port]>/<topic>?secure=yes&priority=high
#       &actions=<ntfy actions>&token=<tk_…>   (or &user=…&pass=…)
#
# It publishes as ntfy documents it: POST (or PUT with the picture) to
# <server>/<topic>, title, message, priority and actions as query
# parameters, the token as "Authorization: Bearer". A newer Apprise sends
# its own ntfy, and this is never used.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import base64
import logging
import os
from typing import Any, Dict, Optional

import requests
from apprise.decorators import notify

SCHEMA = "pntfy"
TIMEOUT = (10., 60.)      # connect, read: same as the component
MESSAGE_MAX = 4096
log = logging.getLogger("apprise")
_native: Optional[bool] = None


def apprise_has_token() -> bool:
    """Whether the installed Apprise's own ntfy takes an access token
    (checked once, by building a URL with one: nothing is sent)."""
    global _native
    if _native is None:
        try:
            import apprise
            obj = apprise.Apprise.instantiate(
                "ntfys://example.invalid/topic?token=tk_check")
            _native = bool(obj) and getattr(obj, "token", None) == "tk_check"
        except Exception:
            _native = False
    return _native


def _attachment(attach: Any) -> Optional[str]:
    for a in attach or []:
        path = a.path if hasattr(a, "path") else str(a)
        if path and os.path.exists(path):
            return path
    return None


def request(meta: Dict[str, Any], body: str, title: str,
            attach: Any) -> Dict[str, Any]:
    """What to send, as requests' keyword arguments (method, url, params,
    headers, and the picture's path): kept apart for the tests."""
    qsd = meta.get("qsd") or {}
    host = meta.get("host") or ""
    port = meta.get("port")
    scheme = "https" if qsd.get("secure", "yes") == "yes" else "http"
    topic = (meta.get("fullpath") or "").strip("/")
    url = f"{scheme}://{host}" + (f":{port}" if port else "") + f"/{topic}"
    params: Dict[str, str] = {}
    if title:
        params["title"] = title
    for key in ("priority", "actions", "tags", "click"):
        if qsd.get(key):
            params[key] = qsd[key]
    headers = {"User-Agent": "pushraker"}
    if qsd.get("token"):
        headers["Authorization"] = f"Bearer {qsd['token']}"
    elif qsd.get("user"):
        pair = f"{qsd['user']}:{qsd.get('pass', '')}".encode("utf-8")
        headers["Authorization"] = "Basic " + \
            base64.b64encode(pair).decode("ascii")
    path = _attachment(attach)
    text = (body or "")[:MESSAGE_MAX]
    if path:
        params["message"] = text
        params["filename"] = os.path.basename(path)
        return {"method": "PUT", "url": url, "params": params,
                "headers": headers, "path": path}
    return {"method": "POST", "url": url, "params": params,
            "headers": headers, "data": text.encode("utf-8")}


def _why(e: Exception) -> str:
    # requests puts the URL in its exception text: only the kind is logged
    if isinstance(e, requests.Timeout):
        return "timed out"
    if isinstance(e, requests.ConnectionError):
        return "could not connect"
    return type(e).__name__


@notify(on=SCHEMA, name="ntfy (with access token)")
def send(body: str, title: str, notify_type: Any, attach: Any,
         meta: Dict[str, Any], *args: Any, **kwargs: Any) -> bool:
    req = request(meta, body, title, attach)
    path = req.pop("path", None)
    method = req.pop("method")
    try:
        if path:
            with open(path, "rb") as f:
                r = requests.request(method, data=f, timeout=TIMEOUT, **req)
        else:
            r = requests.request(method, timeout=TIMEOUT, **req)
    except requests.RequestException as e:
        log.warning("ntfy: %s", _why(e))
        return False
    if r.status_code == 200:
        return True
    code = r.status_code
    try:
        code = int(r.json().get("code") or code)
    except (ValueError, AttributeError, TypeError):
        pass
    # The form Apprise's own ntfy logs, which pushraker turns into a cause
    log.warning("Failed to send ntfy notification: error=%d.", code)
    return False
