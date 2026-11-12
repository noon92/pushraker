# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The settings: what the web page edits, how they are checked, stored and
turned into the component's options (Apprise URLs per notification).

Settings live in <data_path>/config/pushraker.conf, a plain INI file
([pushraker] section) that shows in Fluidd's and Mainsail's config editor
and can be edited by hand or by other tools; the component re-reads it
when it changes. Tokens and passwords live apart, in
<data_path>/pushraker.secrets (mode 0600, outside config/, so Moonraker's
file manager never serves it), as Moonraker does with its own secrets.
"""

from __future__ import annotations
import configparser
import io
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote, urlparse

PROVIDERS = ["ntfy", "gotify", "pushover", "telegram", "discord",
             "discord_dm"]
EMAIL_SECURITY = ["starttls", "ssl", "none"]
PRIORITIES = ["min", "low", "default", "high", "max"]
# Pushover emergency (2) repeats until acknowledged, beyond ntfy's "max";
# both high and max map to Pushover high priority.
PUSHOVER_PRIORITY = {"min": -2, "low": -1, "default": 0, "high": 1, "max": 1}
GOTIFY_PRIORITY = {"min": "low", "low": "moderate", "default": "normal",
                   "high": "high", "max": "emergency"}
# Telegram has no priorities: min and low arrive without a sound
TELEGRAM_SILENT = ("min", "low")
TELEGRAM_API = "https://api.telegram.org/bot"
# Discord: min and low arrive without a sound; the channel mention is only
# added to high and max
DISCORD_PING = ("high", "max")
AUTH_MODES = ["none", "token", "basic"]

# id, label, icon, default push, default priority, image ("render",
# "snapshot", "spool" (drawn spool picture) or None), group, default email
ROWS = [
    ("started", "Print started", "▶️", True, "default", "render", "print",
     False),
    # When the pre-print calibrations chosen for the print job are done
    # (printers that have them)
    ("preprint_done", "Pre-print calibrations finished", "📐", True,
     "default", "render", "print", False),
    ("paused", "Print paused", "⏸️", True, "default", "snapshot", "print",
     False),
    ("resumed", "Print resumed", "▶️", False, "default", "snapshot", "print",
     False),
    # Off by default: for watching the first layer go down after a long
    # start routine (homing, bed mesh, flow calibration)
    ("layer_started", "First layer started", "🎬", False, "default",
     "snapshot", "print", False),
    ("first_layer", "Layer complete", "🔍", False, "default",
     "snapshot", "print", False),
    # At the progress marks chosen under it (progress_marks); sent to the
    # component as its progress_25/50/75 rows
    ("progress", "Progress", "⏳", False, "low", "snapshot", "print", False),
    # Every report_interval minutes of print time (a pause stops the clock)
    ("status_report", "Status report", "⏱️", False, "low", "snapshot",
     "print", False),
    ("complete", "Print complete", "✅", True, "default", "snapshot", "print",
     False),
    ("cancelled", "Print cancelled", "🚫", True, "default", "snapshot",
     "print", False),
    ("macro", "From macros and G-code", "📣", True, "default", "snapshot",
     "print", False),
    ("calibration", "Manual calibrations", "📐", True, "high", "snapshot",
     "print", False),
    # Filaments: loaded into or unloaded from a toolhead,
    # outside a print job
    ("filament_change", "Filament loaded / unloaded", "🧵", False, "low",
     "spool", "filament", False),
    # Need the Spoolman integration
    ("filament_short", "Not enough filament", "🧵", True, "high", "spool",
     "spoolman", True),
    ("filament_low", "Spool running low", "🧵", True, "default", "spool",
     "spoolman", False),
    # The printer's own refill (its toolhead map), with or without
    # Spoolman: Spoolman only adds the spools' names and weights
    ("spool_switch", "Switched to backup spool", "🔄", True, "default",
     "spool", "refill", False),
    # multiACE: the slot feeding a toolhead went empty mid-print
    ("slot_empty", "Spool in ACE slot ran out", "🧵", True, "default",
     "spool", "refill", False),
    # Chamber temperature: only when a print job waits on a chamber target
    # (any chamber heater, or Snapmaker's own chamber wait)
    ("chamber_waiting", "Waiting for chamber temperature", "🌡️", True,
     "default", "snapshot", "chamber", False),
    ("chamber_reached", "Reached chamber temperature", "🌡️", True,
     "default", "snapshot", "chamber", False),
    ("chamber_fault", "Chamber faults", "⚠️", True, "high", "snapshot",
     "chamber", True),
    ("error", "Error", "⚠️", True, "max", "snapshot", "fault", True),
    ("warning", "Warning", "⚠️", True, "max", "snapshot", "fault", True),
    ("shutdown", "Klipper shutdown", "🛑", True, "max", "snapshot", "fault",
     True),
    ("power_loss", "Power loss recovery", "⚡", True, "max", "snapshot", "fault",
     True),
    # X or Y stepper driver at or over the set temperature; once per print
    # job, again after a pause
    ("driver_hot", "Driver running hot", "🌡️", True, "max", "snapshot",
     "fault", True),
]
ROW_IDS = [r[0] for r in ROWS]

# Notification templates (templates/<key>.title.j2 and <key>.j2): key, the
# row whose destinations it uses, its name on the page, the image it sends
# by default and the images it offers. Render: the print job's picture,
# camera: a snapshot, graphic: pushraker's spool picture.
IMAGES = ("none", "render", "camera", "graphic")
JOB_IMAGES = ("none", "render", "camera")
SPOOL_IMAGES = ("none", "render", "camera", "graphic")
TEMPLATES = [
    ("started", "started", "Print started", "render", JOB_IMAGES),
    ("preprint_done", "preprint_done", "Pre-print calibrations finished",
     "render", JOB_IMAGES),
    ("paused", "paused", "Print paused", "camera", JOB_IMAGES),
    ("resumed", "resumed", "Print resumed", "camera", JOB_IMAGES),
    ("layer_started", "layer_started", "First layer started", "camera",
     JOB_IMAGES),
    ("first_layer", "first_layer", "Layer complete", "camera", JOB_IMAGES),
    ("progress", "progress", "Progress", "camera", JOB_IMAGES),
    ("status_report", "status_report", "Status report", "camera",
     JOB_IMAGES),
    ("complete", "complete", "Print complete", "camera", JOB_IMAGES),
    ("cancelled", "cancelled", "Print cancelled", "camera", JOB_IMAGES),
    ("macro", "macro", "From macros and G-code", "camera", JOB_IMAGES),
    ("calibration", "calibration", "Manual calibrations", "camera",
     JOB_IMAGES),
    ("filament_change", "filament_change", "Filament loaded / unloaded",
     "graphic", ("none", "camera", "graphic")),
    ("chamber_waiting", "chamber_waiting", "Waiting for chamber temperature",
     "camera", JOB_IMAGES),
    ("chamber_reached", "chamber_reached", "Reached chamber temperature",
     "camera", JOB_IMAGES),
    ("chamber_fault", "chamber_fault", "Chamber faults", "camera",
     JOB_IMAGES),
    ("error", "error", "Error", "camera", JOB_IMAGES),
    ("warning", "warning", "Warning", "camera", JOB_IMAGES),
    ("shutdown", "shutdown", "Klipper shutdown", "camera", JOB_IMAGES),
    ("power_loss", "power_loss", "Power loss recovery", "camera",
     JOB_IMAGES),
    ("driver_hot", "driver_hot", "Driver running hot", "camera", JOB_IMAGES),
    ("filament_short", "filament_short", "Not enough filament", "graphic",
     SPOOL_IMAGES),
    ("filament_low", "filament_low", "Spool running low", "graphic",
     SPOOL_IMAGES),
    ("spool_switch", "spool_switch", "Switched to backup spool", "graphic",
     SPOOL_IMAGES),
    ("slot_empty", "slot_empty", "Spool in ACE slot ran out", "graphic",
     SPOOL_IMAGES),
]
TEMPLATE_KEYS = [t[0] for t in TEMPLATES]
TEMPLATE = {t[0]: t for t in TEMPLATES}
TEMPLATE_FIELDS = ("title", "contents", "image", "camera", "remaining",
                   "topic")
# A notification's own topic (ntfy: a topic on the same server; Telegram:
# a topic of the chat, by ID, in groups with topics on); "": the default.
# Other services have none.
TOPIC_PROVIDERS = ("ntfy", "telegram")
RX_NTFY_URL = re.compile(r"(ntfys?://[^/?]+/)[^?]*(\?.*)?")
RX_TGRAM_URL = re.compile(r"(tgram://[^/?]+/[^?:]+)(?::\d+)?(\?.*)?")


def topics_not_for(s: Dict[str, Any]) -> List[str]:
    """The notifications whose own topic the push service can't take
    (Telegram: a topic ID, a number)."""
    if s.get("provider") != "telegram":
        return []
    bad = [TEMPLATE[key][2] for key, t in (s.get("templates") or {}).items()
           if key in TEMPLATE and (t or {}).get("topic")
           and not str(t["topic"]).isdigit()]
    bad += [c["name"] for c in s.get("custom") or []
            if c.get("topic") and not str(c["topic"]).isdigit()]
    return bad


def with_topic(url: str, topic: str) -> Optional[str]:
    """A push address sent to another topic (None: the service has none,
    or the topic isn't one it takes)."""
    topic = str(topic or "").strip()
    if not RX_TOPIC.fullmatch(topic):
        return None
    m = RX_NTFY_URL.fullmatch(url)
    if m:
        return f"{m.group(1)}{topic}{m.group(2) or ''}"
    m = RX_TGRAM_URL.fullmatch(url)
    if m and topic.isdigit():
        return f"{m.group(1)}:{topic}{m.group(2) or ''}"
    return None
TITLE_MAX = 1000          # the template, not the title it writes
CONTENTS_MAX = 5000
# The component's names for a row's notifications, to their templates
OPTION_KEYS = dict({f"progress_{m}": "progress" for m in (25, 50, 75)},
                   **{f"macro_{p}": "macro" for p in
                      ("min", "low", "default", "high", "max")})
ROW = {r[0]: r for r in ROWS}
# Pre-print calibrations done (printers that have them)
PREPRINT_ROW = "preprint_done"
# The Progress row's marks, and the component's row for each
PROGRESS_MARKS = (25, 50, 75)
PROGRESS_ROWS = {m: f"progress_{m}" for m in PROGRESS_MARKS}
# Notification data (templating.py): a row adds the default data, its own
# text, or nothing
ERROR_ROWS = ("error", "warning", "shutdown")
# Rows that read Spoolman (the first two need it; the switch works without)
SPOOLMAN_ROWS = ("filament_short", "filament_low", "spool_switch")
SPOOLMAN_ONLY = ("filament_short", "filament_low")
# The rows the Klipper print hooks act on (_PRINT_END_NTFY, _CANCEL_PRINT_NTFY)
HOOK_ROWS = ("complete", "cancelled", "error")

RX_SERVER = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?/?")
RX_TOPIC = re.compile(r"[A-Za-z0-9_\-]{1,64}")
RX_TOKEN = re.compile(r"[A-Za-z0-9_\-]{1,128}")
RX_USER = re.compile(r"[^\s:]{1,64}")
RX_PUSHOVER_KEY = re.compile(r"[A-Za-z0-9]{20,40}")
# Gotify server (may sit under a path behind a proxy) and app token
RX_GOTIFY_SERVER = re.compile(
    r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?")
RX_GOTIFY_TOKEN = re.compile(r"[A-Za-z0-9._\-]{8,64}")
RX_PUSHOVER_DEVICE = re.compile(r"[A-Za-z0-9_\-]{0,25}")
RX_TELEGRAM_TOKEN = re.compile(r"\d{5,16}:[A-Za-z0-9_\-]{30,64}")
RX_TELEGRAM_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")
RX_DISCORD_WEBHOOK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?"
    r"/webhooks/(\d{5,25})/([A-Za-z0-9_\-]{20,100})/?")
RX_DISCORD_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,40}\.[A-Za-z0-9_\-]{4,10}"
                              r"\.[A-Za-z0-9_\-]{20,60}")
RX_DISCORD_ID = re.compile(r"\d{5,25}")
RX_DISCORD_MENTION = re.compile(r"|\d{5,25}|role:\d{5,25}|everyone|here")
RX_WEBHOOK = re.compile(r"https?://[^\s]{3,2000}")
RX_PRINTER_URL = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+(/[^\s]*)?")
RX_NAME = re.compile(r"[\w .()\-]{1,32}")     # \w: letters in any script
RX_CAMERA = re.compile(r"[^\x00-\x1f\x7f]{0,64}")   # a Moonraker webcam name
RX_TIMEZONE = re.compile(r"[A-Za-z0-9_+\-/]{0,64}")
RX_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
RX_HOST = re.compile(r"[A-Za-z0-9.\-]{1,253}")
RX_EMAIL = re.compile(r"[^@\s,;:<>\"'()\[\]\\]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,63}")
RX_EMAIL_USER = re.compile(r"[^\s]{1,128}")
MAX_RECIPIENTS = 5
STRING_FIELDS = ("provider", "server", "topic", "auth", "username",
                 "pushover_user", "pushover_device", "telegram_chat",
                 "gotify_server",
                 "discord_user", "discord_mention", "printer_url",
                 "camera", "name", "timezone", "browser_timezone",
                 "first_layer_objects",
                 "filament_change_events",
                 "complete_objects",
                 "email_host",
                 "email_security", "email_username", "email_from",
                 "email_to", "progress_marks", "time_left")
# Free text with line breaks (stored with \n escaped)
SECRET_FIELDS = ("secret", "email_password", "webhook_url")
INT_FIELDS = ("layers_complete", "email_port", "filament_low_percent",
              "filament_low_grams", "driver_temp", "report_interval")
BOOL_FIELDS = ("enabled", "push_enabled", "email_enabled", "webhook_enabled",
               "filament_unknown")
# Settings the form fills in for common providers
EMAIL_PRESETS = {
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 587,
              "security": "starttls"},
    "brevo": {"label": "Brevo", "host": "smtp-relay.brevo.com", "port": 587,
              "security": "starttls"},
}
# Older settings (the Snapmaker U1 extended firmware's form) named the
# cameras by their place; Moonraker's webcams are named by the user
LEGACY_CAMERAS = {"internal": "case", "usb": "usb"}
SETTINGS_FORMAT = 1


class ConfigError(Exception):
    """A setting that can't be used; `field`: which (its pushraker.conf
    name), when it's one."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class Env:
    """What the printer offers, found at runtime by the component (or the
    harness): the settings can't know it on their own.

      cameras       Moonraker's enabled webcams: name -> {"snapshot": URL
                    the component fetches (absolute), "stream": the live
                    view's path or URL for the notification's link}
      spoolman_url  Spoolman's base URL when Moonraker has [spoolman], else ""
      error_index_url  the printer maker's error-code index, linked from
                    fault notifications ("" for none)
      name          the printer's default name
      rows_unavailable  rows the printer can't produce (off by default and
                    left off the page unless turned on in pushraker.conf)
      preprint      the printer has pre-print calibrations (the Print
                    started row's checkbox)
      time_methods  [{"id", "label", "help"}] the time-left methods on
                    offer, and time_default the printer's default
    """

    def __init__(self, cameras: Optional[Dict[str, Dict[str, str]]] = None,
                 spoolman_url: str = "",
                 error_index_url: str = "", name: str = "Printer",
                 rows_unavailable: Sequence[str] = (),
                 preprint: bool = False,
                 time_methods: Optional[List[Dict[str, str]]] = None,
                 time_default: str = "blend") -> None:
        self.cameras = cameras or {}
        self.time_methods = time_methods or []
        self.time_default = time_default
        self.preprint = preprint
        self.rows_unavailable = set(rows_unavailable)
        self.spoolman_url = spoolman_url.rstrip("/")
        self.error_index_url = error_index_url
        self.name = name


# ---- defaults and storage ---------------------------------------------

def defaults(name: str = "Printer",
             unavailable: Sequence[str] = ()) -> Dict[str, Any]:
    """The settings a new install starts with. Rows the printer can't
    produce start off (push and email), so they don't show on its page;
    turned on in pushraker.conf, they show and work as any other."""
    s = _defaults(name)
    for rid in unavailable:
        if rid in s["rows"]:
            s["rows"][rid]["enabled"] = False
            s["rows"][rid]["email"] = False
    return s


def _defaults(name: str) -> Dict[str, Any]:
    return {
        "format": SETTINGS_FORMAT,
        "enabled": False,
        # Push, email and the webhook are each behind their own checkbox
        "push_enabled": True,
        "provider": "ntfy",
        "server": "",
        "topic": "printer",
        "auth": "none",
        "username": "",
        "pushover_user": "",
        "pushover_device": "",
        "gotify_server": "",
        "telegram_chat": "",
        "discord_user": "",
        "discord_mention": "",
        "secret": "",
        "secret_for": "",       # the service the push secret belongs to
        "email_password_for": "",   # the SMTP server and account it's for
        "printer_url": "",
        "camera": "",           # a Moonraker webcam name, "" for none
        "name": name,
        # "browser": the time zone of the browser the page was last saved
        # from (browser_timezone); or a time zone of its own
        "timezone": "browser",
        "browser_timezone": "",
        "layers_complete": 2,
        # Print-by-object: "layer N complete" and "Print complete" for
        # each object, or once per print job
        "first_layer_objects": "each",
        "filament_change_events": "loaded,unloaded",
        "complete_objects": "each",
        "filament_low_percent": 10,
        "filament_low_grams": 50,
        "filament_unknown": True,
        # The Progress row: which of 25, 50, 75 % it reports
        "progress_marks": "25,50,75",
        # Time-left method (component.TIME_METHODS and the printer's own);
        # "" for the printer's default
        "time_left": "",
        "driver_temp": 105,     # °C, X or Y stepper driver (row: driver_hot)
        "report_interval": 30,  # minutes of print time between status reports
        "email_enabled": False,
        "webhook_enabled": False,
        "webhook_url": "",
        "email_host": "",
        "email_port": 587,
        "email_security": "starttls",
        "email_username": "",
        "email_password": "",
        "email_from": "",
        "email_to": "",
        "rows": {r[0]: {"enabled": r[3], "priority": r[4],
                        "email": r[7]}
                 for r in ROWS},
        # Each notification's template and image, where they differ from
        # the default: {key: {title, contents, image, camera, remaining}}.
        # What isn't here follows the default, also after an upgrade.
        "templates": {},
        # Notifications of the user's own: [pushraker custom <name>]
        "custom": [],
    }


def escape_text(text: str) -> str:
    """Line breaks on one INI line."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def unescape_text(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n"
                  else m.group(1), text)


SECTION = "pushraker"
CUSTOM_SECTION = "pushraker custom "

# Custom notifications: a name (letters, digits, _; it's how G-code fires
# one: RESPOND PREFIX=notify_custom MSG=<name>), a condition (a Jinja
# expression, checked every few seconds: sent when it turns true), and a
# template like the built-in ones
CUSTOM_MAX = 20
RX_CUSTOM_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
CUSTOM_IMAGES = ("none", "render", "camera")
CUSTOM_REPEATS = ("change", "job", "job_pause", "minutes")
WHEN_MAX = 1000
CUSTOM_DEFAULT = {
    "enabled": True, "priority": "default", "email": False,
    "when": "",
    "title": "🔔 {{ machine.name }} {{ event.name }}",
    # Empty: the editor shows an example; sent empty, the title stands alone
    "contents": "",
    "image": "camera", "camera": "", "topic": "",
    "repeat": "change", "repeat_minutes": 10,
}


def custom_rid(name: str) -> str:
    """The component's name for a custom notification."""
    return f"custom_{name}"


def when_template(when: str) -> str:
    """A condition as a template that reads "1" when it's true."""
    return "{% if (" + (when or "false") + ") %}1{% endif %}"


def clean_custom(items: Any, strict: bool = False) -> List[Dict[str, Any]]:
    """The custom notifications, checked. strict (saving from the page):
    a mistake is refused with what and where; otherwise the bad one is
    left out."""
    from . import engine
    if not isinstance(items, list):
        if strict:
            raise ConfigError("Invalid request")
        return []
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in items[:CUSTOM_MAX] if not strict else items:
        try:
            if not isinstance(raw, dict):
                raise ConfigError("Invalid request")
            c = dict(CUSTOM_DEFAULT)
            name = str(raw.get("name") or "").strip()
            if not RX_CUSTOM_NAME.fullmatch(name):
                raise ConfigError(
                    f'Custom notification "{name}": the name is up to 32 '
                    "letters, digits and _, starting with a letter")
            if name.lower() in seen:
                raise ConfigError(f'Two custom notifications are named '
                                  f'"{name}"')
            c["name"] = name
            c["enabled"] = bool(raw.get("enabled", c["enabled"]))
            c["email"] = bool(raw.get("email", c["email"]))
            if raw.get("priority") in PRIORITIES:
                c["priority"] = raw["priority"]
            c["when"] = clean_text(raw.get("when", ""), WHEN_MAX).strip()
            c["title"] = clean_text(raw.get("title", c["title"]), TITLE_MAX)
            c["contents"] = clean_text(raw.get("contents", c["contents"]),
                                       CONTENTS_MAX)
            image = str(raw.get("image") or c["image"])
            if image not in CUSTOM_IMAGES:
                raise ConfigError(f"{name}: no such image")
            c["image"] = image
            cam = str(raw.get("camera") or "")
            if not RX_CAMERA.fullmatch(cam):
                raise ConfigError("Invalid camera")
            c["camera"] = cam
            topic = str(raw.get("topic") or "").strip()
            if topic and not RX_TOPIC.fullmatch(topic):
                raise ConfigError(f"{name}: a topic is up to 64 letters, "
                                  "digits, - and _")
            c["topic"] = topic
            repeat = str(raw.get("repeat") or "change")
            if repeat not in CUSTOM_REPEATS:
                raise ConfigError(f"{name}: no such repeat")
            c["repeat"] = repeat
            try:
                minutes = int(raw.get("repeat_minutes", 10))
            except (TypeError, ValueError):
                minutes = 0
            if not 1 <= minutes <= 1440:
                raise ConfigError(f"{name}: repeat every 1 to 1440 minutes")
            c["repeat_minutes"] = minutes
            for field, text in (("When", when_template(c["when"])),
                                ("title", c["title"]),
                                ("contents", c["contents"])):
                left = engine.RX_PLACEHOLDER.search(
                    c["when"] if field == "When" else text)
                if left:
                    raise ConfigError(f"{name}: replace # in "
                                      f"{left.group(0)} with a number")
                bad = engine.check(text)
                if bad:
                    raise ConfigError(f"{name}, {field}: {bad}")
        except ConfigError:
            if strict:
                raise
            continue
        seen.add(name.lower())
        out.append(c)
    if strict and len(out) > CUSTOM_MAX:
        raise ConfigError(f"Up to {CUSTOM_MAX} custom notifications")
    return out


def custom_lines(s: Dict[str, Any]) -> List[str]:
    """The custom notifications as [pushraker custom <name>] sections."""
    lines: List[str] = []
    for c in s.get("custom") or []:
        lines += ["", f"[{CUSTOM_SECTION}{c['name']}]",
                  f"notification: {'on' if c['enabled'] else 'off'}, "
                  f"{c['priority']}, {'email' if c['email'] else 'no_email'}",
                  f"when: {escape_text(c['when'])}",
                  f"title: {escape_text(c['title'])}",
                  f"contents: {escape_text(c['contents'])}",
                  f"image: {c['image']}",
                  f"camera: {c['camera']}",
                  f"topic: {c['topic']}",
                  "repeat: " + (str(c["repeat_minutes"])
                                if c["repeat"] == "minutes"
                                else c["repeat"])]
    return lines


def parse_custom(cfg: Any) -> List[Dict[str, Any]]:
    """The [pushraker custom <name>] sections of pushraker.conf."""
    items = []
    for sec_name in cfg.sections():
        if not sec_name.startswith(CUSTOM_SECTION):
            continue
        sec = cfg[sec_name]
        c: Dict[str, Any] = {"name": sec_name[len(CUSTOM_SECTION):].strip()}
        row = {"enabled": True, "priority": "default", "email": False}
        if sec.get("notification"):
            _parse_row(row, sec.get("notification"))
        c.update(row)
        for key in ("when", "title", "contents"):
            if key in sec:
                c[key] = unescape_text(sec.get(key).strip())
        for key in ("image", "camera", "topic"):
            if key in sec:
                c[key] = sec.get(key).strip()
        repeat = (sec.get("repeat") or "change").strip().lower()
        if repeat.isdigit():
            c["repeat"], c["repeat_minutes"] = "minutes", int(repeat)
        else:
            c["repeat"] = repeat
        items.append(c)
    return clean_custom(items)


SECRETS_SECTION = "pushraker"
CONF_NAME = "pushraker.conf"
SECRETS_NAME = "pushraker.secrets"


def section_lines(s: Dict[str, Any]) -> List[str]:
    """pushraker.conf as written: every setting on its own line, the
    rows as on|off, priority, image|no_image, email|no_email."""
    lines = [
        f"[{SECTION}]",
        "# pushraker settings. Edit here or on the notifications page; the",
        "# component re-reads this file when it changes. Tokens and passwords",
        f"# are in {SECRETS_NAME} next to printer_data/config (never here).",
        f"format: {SETTINGS_FORMAT}",
    ]
    for key in BOOL_FIELDS:
        lines.append(f"{key}: {'true' if s[key] else 'false'}")
    for key in STRING_FIELDS:
        lines.append(f"{key}: {s[key]}")
    for key in INT_FIELDS:
        lines.append(f"{key}: {s[key]}")
    lines.append("# <notification>: on|off (push), <priority>, "
                 "email|no_email")
    for rid in ROW_IDS:
        r = s["rows"][rid]
        lines.append(f"{rid}: {'on' if r['enabled'] else 'off'}, "
                     f"{r['priority']}, "
                     f"{'email' if r['email'] else 'no_email'}")
    lines.append("# Templates (Jinja; \\n for a line break) and images, "
                 "where they differ from the")
    lines.append("# defaults: <notification>_title, _contents, _image "
                 "(none|render|camera|graphic),")
    lines.append("# _camera (a Moonraker webcam name), _remaining "
                 "(true|false: filament left on the graphic), _topic "
                 "(ntfy, Telegram)")
    for key in TEMPLATE_KEYS:
        t = s["templates"].get(key) or {}
        for field in TEMPLATE_FIELDS:
            if field not in t:
                continue
            v = t[field]
            v = ("true" if v else "false") if field == "remaining" else \
                escape_text(v) if field in ("title", "contents") else v
            lines.append(f"{key}_{field}: {v}")
    lines += custom_lines(s)
    return [ln + "\n" for ln in lines]


def parse_section(sec: Any, s: Dict[str, Any]) -> Dict[str, Any]:
    """A [pushraker] (or the old [notifications]) INI section over `s`."""
    for key in BOOL_FIELDS:
        if key in sec:
            s[key] = sec.get(key, "").strip().lower() == "true"
    for key in STRING_FIELDS + SECRET_FIELDS:
        if key in sec:
            s[key] = sec.get(key).strip()
    if s["provider"] == "none":
        s["push_enabled"] = False    # older settings: no push service
        s["provider"] = "ntfy"
    for key in INT_FIELDS:
        try:
            s[key] = int(sec.get(key, str(s[key])))
        except ValueError:
            pass
    if "timezone" in sec and "browser_timezone" not in sec:
        # Before the setting: the page saved the browser's time zone
        s["browser_timezone"] = s["timezone"] if s["timezone"] not in (
            "", "browser") else ""
        s["timezone"] = "browser"
    for rid in ROW_IDS:
        raw = sec.get(rid)
        if rid == PREPRINT_ROW and raw and raw.strip().lower() in (
                "true", "false"):
            # Before its row: a checkbox under Print started, with its
            # settings
            s["rows"][rid].update(dict(s["rows"]["started"]),
                                  enabled=raw.strip().lower() == "true"
                                  and s["rows"]["started"]["enabled"])
            if raw.strip().lower() == "false":
                s["rows"][rid]["email"] = False
            continue
        if raw:
            if _parse_row(s["rows"][rid], raw) is False:
                # Before templates: no_image on the row: its notifications
                # send none
                for t in TEMPLATES:
                    if t[1] == rid:
                        s["templates"].setdefault(t[0], {})["image"] = "none"
    # Notification data (before templates) isn't carried over: its tokens
    # aren't Jinja; templates replace it
    for key in TEMPLATE_KEYS:
        t: Dict[str, Any] = {}
        for field in TEMPLATE_FIELDS:
            raw = sec.get(f"{key}_{field}")
            if raw is None:
                continue
            raw = raw.strip()
            if field == "remaining":
                t[field] = raw.lower() == "true"
            elif field in ("title", "contents"):
                t[field] = unescape_text(raw)
            else:
                t[field] = raw
        if t:
            s["templates"].setdefault(key, {}).update(t)
    s["templates"] = clean_templates(s["templates"])
    # Settings from before the Progress row: 25% / 50% / 75% complete were
    # rows of their own; the first one on gives the row its settings
    old = [(m, sec.get(PROGRESS_ROWS[m])) for m in PROGRESS_MARKS]
    if any(raw for _, raw in old) and not sec.get("progress"):
        rows = {m: _parse_row(dict(s["rows"]["progress"]), raw)
                for m, raw in old if raw}
        on = [m for m, r in rows.items() if r["enabled"] or r["email"]]
        s["progress_marks"] = ",".join(str(m) for m in on) \
            if on else s["progress_marks"]
        first = rows[on[0]] if on else rows[min(rows)]
        s["rows"]["progress"].update(
            enabled=first["enabled"], priority=first["priority"],
            email=first["email"])
    s["camera"] = LEGACY_CAMERAS.get(s["camera"], s["camera"])
    return s


def _parse_row(row: Dict[str, Any], raw: str) -> Any:
    """on|off, priority, email|no_email (before templates also
    image|no_image: False when it said no_image)."""
    parts = [p.strip().lower() for p in raw.split(",")]
    row["enabled"] = parts[0] == "on"
    image = None
    for p in parts[1:]:
        if p in PRIORITIES:
            row["priority"] = p
        elif p in ("email", "no_email"):
            row["email"] = p == "email"
        elif p in ("image", "no_image"):
            image = p == "image"
    return False if image is False else row


def read_ini(path: str) -> configparser.RawConfigParser:
    cfg = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cfg.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        pass
    return cfg


class Store:
    """pushraker.conf (everything but the secrets) and pushraker.secrets
    (0600)."""

    def __init__(self, config_dir: str, data_dir: str,
                 name: str = "Printer") -> None:
        self.settings_path = os.path.join(config_dir, CONF_NAME)
        self.secrets_path = os.path.join(data_dir, SECRETS_NAME)
        self.default_name = name
        # Rows the printer can't produce (set once it is known): they start
        # off when there is no settings file yet
        self.unavailable: Sequence[str] = ()
        self._stamp: Any = None

    def exists(self) -> bool:
        return os.path.exists(self.settings_path)

    def stamp(self) -> Any:
        """Changes when either file does (for re-reading edits)."""
        out = []
        for p in (self.settings_path, self.secrets_path):
            try:
                st = os.stat(p)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def changed(self) -> bool:
        """Whether the files changed since the last load or save."""
        return self.stamp() != self._stamp

    def load(self) -> Dict[str, Any]:
        s = defaults(self.default_name, self.unavailable)
        cfg = read_ini(self.settings_path)
        if cfg.has_section(SECTION):
            parse_section(cfg[SECTION], s)
        s["custom"] = parse_custom(cfg)
        secrets = _read_ini_or_json_section(self.secrets_path, SECRETS_SECTION)
        for key in SECRET_FIELDS + ("secret_for", "email_password_for"):
            if secrets.get(key):
                s[key] = str(secrets[key])
        # Settings from before the secrets were scoped: the secrets belong
        # to the service and server they were saved with
        if s["secret"] and not s["secret_for"]:
            s["secret_for"] = secret_scope(s)
        if s["email_password"] and not s["email_password_for"]:
            s["email_password_for"] = email_scope(s)
        self._stamp = self.stamp()
        return s

    def save(self, s: Dict[str, Any]) -> None:
        """Save both files, restoring the old secrets if settings fail."""
        secrets = {k: s[k] for k in SECRET_FIELDS if s.get(k)}
        for key, of in (("secret_for", "secret"),
                        ("email_password_for", "email_password")):
            if s.get(key) and s.get(of):
                secrets[key] = s[key]
        cfg = configparser.RawConfigParser(strict=False, interpolation=None)
        if secrets:
            cfg.add_section(SECRETS_SECTION)
            for k, v in secrets.items():
                cfg.set(SECRETS_SECTION, k, v)
        buf = io.StringIO()
        cfg.write(buf)
        try:
            with open(self.secrets_path, encoding="utf-8") as f:
                old_secrets = f.read()
        except FileNotFoundError:
            old_secrets = ""
        self._write(self.secrets_path, buf.getvalue() if secrets else "",
                    0o600)
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            self._write(self.settings_path, "".join(section_lines(s)), 0o644)
        except OSError as save_error:
            try:
                self._write(self.secrets_path, old_secrets, 0o600)
            except OSError as restore_error:
                raise OSError("Settings save failed and the previous secrets "
                              f"could not be restored: {restore_error}") from save_error
            raise
        self._stamp = self.stamp()

    @staticmethod
    def _write(path: str, text: str, mode: int) -> None:
        if not text:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        try:
            st = os.stat(path)
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, path)


def _read_ini_or_json_section(path: str, section: str) -> Dict[str, str]:
    """A section of an INI file, or a key of a JSON object (Moonraker's
    secrets file can be either)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            sec = data.get(section) or {}
            return {k: str(v) for k, v in sec.items()} \
                if isinstance(sec, dict) else {}
    except ValueError:
        pass
    cfg = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cfg.read_string(text)
    except configparser.Error:
        return {}
    return dict(cfg[section]) if cfg.has_section(section) else {}


def import_ini(settings_file: str, section: str,
               secrets_file: str = "", secrets_section: str = "",
               name: str = "Printer") -> Dict[str, Any]:
    """Settings from an INI section as the Snapmaker U1 extended firmware's
    form wrote them (extended2.cfg [notifications], secrets in Moonraker's
    secrets file, INI or JSON), for the move to pushraker.conf."""
    cfg = read_ini(settings_file)
    s = defaults(name)
    if not cfg.has_section(section):
        return s
    parse_section(cfg[section], s)
    if secrets_file:
        stored = _read_ini_or_json_section(secrets_file, secrets_section)
        for key in SECRET_FIELDS + ("secret_for", "email_password_for"):
            if stored.get(key):
                s[key] = str(stored[key])
    if s["secret"] and not s["secret_for"]:
        s["secret_for"] = secret_scope(s)
    if s["email_password"] and not s["email_password_for"]:
        s["email_password_for"] = email_scope(s)
    return s


# ---- validation ----------------------------------------------------------

def validate(data: Dict[str, Any], current: Dict[str, Any],
             env: Optional[Env] = None) -> Dict[str, Any]:
    """Merge submitted JSON over current settings and validate."""
    if not isinstance(data, dict):
        raise ConfigError("Invalid request")
    s = json.loads(json.dumps(current))
    for key in BOOL_FIELDS:
        if key in data:
            s[key] = bool(data[key])
    if "secret" in data and str(data["secret"]):
        s["secret_for"] = ""     # set below, once the service is known
    if "email_password" in data and str(data["email_password"]):
        s["email_password_for"] = ""
    for key in STRING_FIELDS:
        if key in data:
            s[key] = str(data[key] or "").strip()
    for key in SECRET_FIELDS:
        if data.get(key):
            s[key] = str(data[key])
    for key, lo, hi in (("filament_low_percent", 0, 90),
                        ("filament_low_grams", 0, 5000)):
        if key in data:
            try:
                s[key] = int(data[key] or 0)
            except (TypeError, ValueError):
                raise ConfigError("Filament alert values must be numbers", "filament_low_percent")
        if not lo <= s[key] <= hi:
            raise ConfigError("Filament alert: 0-90 % and 0-5000 g", "filament_low_percent")
    if "email_port" in data:
        try:
            s["email_port"] = int(data["email_port"])
        except (TypeError, ValueError):
            raise ConfigError("Email port must be a number", "email_port")
    if "layers_complete" in data:
        try:
            s["layers_complete"] = int(data["layers_complete"])
        except (TypeError, ValueError):
            raise ConfigError("Layer must be 1, 2 or 3", "layers_complete")
    if "driver_temp" in data:
        try:
            s["driver_temp"] = int(data["driver_temp"])
        except (TypeError, ValueError):
            raise ConfigError("Driver temperature must be a number", "driver_temp")
    if not 40 <= s["driver_temp"] <= 150:
        raise ConfigError("Driver running hot: the temperature must be 40 to 150 °C", "driver_temp")
    if "report_interval" in data:
        try:
            s["report_interval"] = int(data["report_interval"])
        except (TypeError, ValueError):
            raise ConfigError("Status report interval must be a number", "report_interval")
    if not 5 <= s["report_interval"] <= 720:
        raise ConfigError("Status report: every 5 to 720 minutes of print time", "report_interval")
    rows = data.get("rows") or {}
    if not isinstance(rows, dict):
        raise ConfigError("Invalid request")
    for rid, row in rows.items():
        if rid not in s["rows"] or not isinstance(row, dict):
            continue
        r = s["rows"][rid]
        r["enabled"] = bool(row.get("enabled", r["enabled"]))
        if row.get("priority") in PRIORITIES:
            r["priority"] = row["priority"]
        r["email"] = bool(row.get("email", r["email"]))
    if "templates" in data:
        tpls = data["templates"]
        if not isinstance(tpls, dict):
            raise ConfigError("Invalid request")
        for key, t in tpls.items():
            if key not in TEMPLATE or not isinstance(t, (dict, type(None))):
                continue
            if not t:
                s["templates"].pop(key, None)        # reset to the default
                continue
            new = dict(s["templates"].get(key) or {})
            for field in TEMPLATE_FIELDS:
                if field in t:
                    new[field] = t[field]
            s["templates"][key] = new
        s["templates"] = clean_templates(s["templates"], strict=True)
    if "custom" in data:
        s["custom"] = clean_custom(data["custom"], strict=True)
    bad = topics_not_for(s)
    if bad:
        raise ConfigError("Telegram topics are IDs (numbers): change or "
                          "clear the topic of " + ", ".join(bad), "topic")
    if s["time_left"] == "first_layer":       # 0.3.1's name
        s["time_left"] = "slicer_first_layer"
    if s["time_left"] and env is not None and env.time_methods and \
            s["time_left"] not in {m["id"] for m in env.time_methods}:
        raise ConfigError("Unknown time-left method")
    if env is not None and s["time_left"] == env.time_default:
        s["time_left"] = ""          # the printer's default: follow it
    marks = [m.strip() for m in s["progress_marks"].split(",") if m.strip()]
    if any(m not in ("25", "50", "75") for m in marks):
        raise ConfigError("Progress: 25, 50 and/or 75 %")
    s["progress_marks"] = ",".join(
        str(m) for m in PROGRESS_MARKS if str(m) in marks)

    for key in STRING_FIELDS + SECRET_FIELDS:
        if RX_CONTROL.search(s[key]):
            raise ConfigError(f"{key}: control characters are not allowed")
    s["server"] = s["server"].rstrip("/")
    s["email_password"] = s["email_password"].strip()
    # The push secret is one field for every service (ntfy token or
    # password, Pushover app token, Telegram bot token, Discord token or
    # webhook). A stored one is only reused for the service it was entered
    # for: switching services means entering the new one. (Settings from
    # before kept the server too: "ntfy|token|https://…|"; the service is
    # the part that counts.)
    scope = secret_scope(s)
    if s["secret"] and s.get("secret_for", "").split("|")[0] \
            not in ("", scope):
        s["secret"] = ""
        if s["push_enabled"]:
            raise ConfigError(
                "The saved token or password was entered for another "
                "service. Enter the one for this one.")
    s["secret_for"] = scope if s["secret"] else ""
    if s["provider"] not in PROVIDERS:
        raise ConfigError("Invalid notification service")
    if s["auth"] not in AUTH_MODES:
        raise ConfigError("Invalid authentication mode")
    s["camera"] = LEGACY_CAMERAS.get(s["camera"], s["camera"])
    if not RX_CAMERA.fullmatch(s["camera"]):
        raise ConfigError("Invalid camera")
    if s["layers_complete"] not in (1, 2, 3):
        raise ConfigError("Layer must be 1, 2 or 3", "layers_complete")
    for key in ("first_layer_objects", "complete_objects"):
        if s[key] not in ("each", "once"):
            raise ConfigError("When printing by object: once per object "
                              "or once per print job")
    events = [e for e in s["filament_change_events"].split(",") if e]
    if any(e not in ("loaded", "unloaded") for e in events):
        raise ConfigError("Filament loaded / unloaded: loaded, unloaded")
    s["filament_change_events"] = ",".join(events)
    if not RX_NAME.fullmatch(s["name"]):
        raise ConfigError("Printer name: up to 32 letters, digits, spaces "
                          "and . _ - ( )")
    if not s["timezone"]:
        s["timezone"] = "browser"
    if not RX_TIMEZONE.fullmatch(s["timezone"]) or \
            not RX_TIMEZONE.fullmatch(s["browser_timezone"]):
        raise ConfigError("Invalid time zone")
    if s["printer_url"] and not RX_PRINTER_URL.fullmatch(s["printer_url"]):
        raise ConfigError("Printer URL must look like http://100.64.0.1")
    if s["printer_url"]:
        try:
            u = urlparse(s["printer_url"])
            if not u.hostname or (u.port is not None
                                  and not 0 < u.port < 65536):
                raise ValueError("host or port")
        except ValueError:
            raise ConfigError("Printer URL must look like http://100.64.0.1")
    validate_email(s)
    if s["webhook_enabled"]:
        try:
            u = urlparse(s["webhook_url"])
            ok = bool(RX_WEBHOOK.fullmatch(s["webhook_url"]) and u.hostname
                      and (u.port is None or 0 < u.port < 65536))
        except ValueError:
            ok = False
        if not ok:
            raise ConfigError("Webhook URL must look like "
                              "https://example.com/hook")
    if s["enabled"] and not s["push_enabled"] and \
            not s["email_enabled"] and not s["webhook_enabled"]:
        raise ConfigError("Enable push notifications, email or a webhook")
    if not s["push_enabled"]:
        pass
    elif s["provider"] == "ntfy":
        if s["enabled"] or s["server"]:
            if not RX_SERVER.fullmatch(s["server"]):
                raise ConfigError(
                    "Server must look like https://ntfy.example.com or "
                    "http://192.168.1.10:8080 (no path)")
            if not RX_TOPIC.fullmatch(s["topic"]):
                raise ConfigError("Topic: 1-64 letters, digits, - or _")
            if is_public(s["server"]) and len(s["topic"]) < 16:
                raise ConfigError("On the public ntfy.sh server use a topic "
                                  "of at least 16 characters")
        if s["auth"] == "token":
            if not RX_TOKEN.fullmatch(s["secret"]):
                raise ConfigError("Access token is missing or invalid")
        elif s["auth"] == "basic":
            if not RX_USER.fullmatch(s["username"]):
                raise ConfigError("Username is missing or invalid")
            if not s["secret"] or s["secret"] != s["secret"].strip():
                raise ConfigError("Password is missing or has leading or "
                                  "trailing spaces")
        if s["auth"] != "basic":
            s["username"] = ""
        if s["auth"] == "none":
            s["secret"] = ""
    elif s["provider"] == "gotify":
        if s["enabled"] or s["gotify_server"]:
            if not RX_GOTIFY_SERVER.fullmatch(s["gotify_server"]):
                raise ConfigError("Gotify server must look like "
                                  "https://gotify.example.com")
            try:
                urlparse(s["gotify_server"]).port
            except ValueError:
                raise ConfigError("Gotify server: port out of range")
        if s["enabled"] or s["secret"]:
            if not RX_GOTIFY_TOKEN.fullmatch(s["secret"]):
                raise ConfigError("Gotify app token is missing or invalid "
                                  "(Gotify web UI > Apps > Create "
                                  "Application)")
    elif s["provider"] == "telegram":
        if s["enabled"] or s["secret"]:
            if not RX_TELEGRAM_TOKEN.fullmatch(s["secret"]):
                raise ConfigError("Telegram bot token is missing or invalid "
                                  "(from @BotFather, like 123456789:ABC...)")
        if s["telegram_chat"] and \
                not RX_TELEGRAM_CHAT.fullmatch(s["telegram_chat"]):
            raise ConfigError("Telegram chat ID: a number like 123456789 "
                              "(or -100... for a group), or @channelname")
    elif s["provider"] == "discord":
        if s["enabled"] or s["secret"]:
            if not RX_DISCORD_WEBHOOK.fullmatch(s["secret"]):
                raise ConfigError("Discord webhook URL is missing or invalid "
                                  "(https://discord.com/api/webhooks/...)")
        if not RX_DISCORD_MENTION.fullmatch(s["discord_mention"]):
            raise ConfigError("Discord mention: a user ID, role:<role ID>, "
                              "everyone or here (or empty)")
    elif s["provider"] == "discord_dm":
        if s["enabled"] or s["secret"]:
            if not RX_DISCORD_TOKEN.fullmatch(s["secret"]):
                raise ConfigError("Discord bot token is missing or invalid "
                                  "(Developer Portal > your app > Bot > "
                                  "Reset Token)")
        if s["discord_user"] and not RX_DISCORD_ID.fullmatch(
                s["discord_user"]):
            raise ConfigError("Discord user ID: digits only")
    else:
        if s["enabled"] or s["pushover_user"]:
            if not RX_PUSHOVER_KEY.fullmatch(s["pushover_user"]):
                raise ConfigError("Pushover user key is missing or invalid")
            if not RX_PUSHOVER_KEY.fullmatch(s["secret"]):
                raise ConfigError("Pushover API token is missing or invalid")
        if not RX_PUSHOVER_DEVICE.fullmatch(s["pushover_device"]):
            raise ConfigError("Pushover device: up to 25 letters, digits, "
                              "- or _")
    return s


def clean_text(value: Any, cap: int) -> str:
    """A template as typed: line breaks and tabs kept, other control
    characters dropped, capped."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or not RX_CONTROL.match(c))
    if len(text) > cap:
        raise ConfigError(f"Templates: up to {cap} characters")
    return text.rstrip()


def template_default(key: str) -> Dict[str, Any]:
    """A notification's default template, image and options."""
    from . import defaults
    title, contents = defaults.default(key) or ("", "")
    return {"title": title, "contents": contents,
            "image": TEMPLATE[key][3], "camera": "", "remaining": True,
            "topic": ""}


def clean_templates(tpls: Dict[str, Any],
                    strict: bool = False) -> Dict[str, Any]:
    """Each notification's edits, checked: what equals the default is
    dropped (it follows the default again). strict (saving from the
    page): a template Jinja can't read is refused, with where."""
    from . import engine
    out: Dict[str, Any] = {}
    for key, t in (tpls or {}).items():
        if key not in TEMPLATE or not isinstance(t, dict):
            continue
        d = template_default(key)
        kept: Dict[str, Any] = {}
        for field in TEMPLATE_FIELDS:
            if field not in t:
                continue
            v = t[field]
            if field == "title":
                v = clean_text(v, TITLE_MAX)
            elif field == "contents":
                v = clean_text(v, CONTENTS_MAX)
            elif field == "image":
                v = str(v)
                if v not in TEMPLATE[key][4]:
                    if strict:
                        raise ConfigError(f"{TEMPLATE[key][2]}: no such "
                                          "image")
                    continue
            elif field == "camera":
                v = str(v or "")
                if not RX_CAMERA.fullmatch(v):
                    raise ConfigError("Invalid camera")
            elif field == "topic":
                v = str(v or "").strip()
                if v and not RX_TOPIC.fullmatch(v):
                    if strict:
                        raise ConfigError(f"{TEMPLATE[key][2]}: a topic is "
                                          "up to 64 letters, digits, - "
                                          "and _")
                    continue
            else:
                v = bool(v)
            if field in ("title", "contents") and strict:
                left = engine.RX_PLACEHOLDER.search(v)
                if left:
                    raise ConfigError(f"{TEMPLATE[key][2]}: replace # in "
                                      f"{left.group(0)} with a number")
                bad = engine.check(v)
                if bad:
                    raise ConfigError(f"{TEMPLATE[key][2]}, "
                                      f"{field}: {bad}")
            if v != d[field]:
                kept[field] = v
        if kept:
            out[key] = kept
    return out


def template(s: Dict[str, Any], key: str) -> Dict[str, Any]:
    """A notification's template as it stands: its edits over the
    default."""
    t = template_default(key)
    t.update((s.get("templates") or {}).get(key) or {})
    return t


def progress_marks(s: Dict[str, Any]) -> List[int]:
    return [m for m in PROGRESS_MARKS if str(m) in s["progress_marks"].split(",")]


def recipients(s: Dict[str, Any]) -> List[str]:
    return [a.strip() for a in s["email_to"].split(",") if a.strip()]


def validate_email(s: Dict[str, Any]) -> None:
    # Only when email is on: the form always sends its preset server, and
    # half-filled email settings must not block saving push settings.
    if not s["email_enabled"]:
        return
    if s["email_security"] not in EMAIL_SECURITY:
        raise ConfigError("Invalid email security")
    if not 1 <= s["email_port"] <= 65535:
        raise ConfigError("Email port must be 1-65535")
    if not RX_HOST.fullmatch(s["email_host"]):
        raise ConfigError("SMTP server must be a host name like "
                          "smtp.example.com")
    if s["email_username"] and not RX_EMAIL_USER.fullmatch(
            s["email_username"]):
        raise ConfigError("Email username must not contain spaces")
    scope = email_scope(s)
    if s["email_password"] and s.get("email_password_for", "") \
            not in ("", scope):
        s["email_password"] = ""
        raise ConfigError(
            "The saved email password was entered for another server or "
            "account. Enter it again.")
    s["email_password_for"] = scope if s["email_password"] else ""
    if s["email_username"] and not s["email_password"]:
        raise ConfigError("Email password is missing")
    if not RX_EMAIL.fullmatch(s["email_from"]):
        raise ConfigError("From must be an email address")
    to = recipients(s)
    if not to or len(to) > MAX_RECIPIENTS or \
            not all(RX_EMAIL.fullmatch(a) for a in to):
        raise ConfigError(f"To: 1 to {MAX_RECIPIENTS} email addresses, "
                          "separated by commas")
    s["email_to"] = ", ".join(to)
    if not s["email_username"]:
        s["email_password"] = ""
        s["email_password_for"] = ""


def is_public(server: str) -> bool:
    return urlparse(server).hostname in ("ntfy.sh", "www.ntfy.sh")


def email_scope(s: Dict[str, Any]) -> str:
    """What a stored email password is bound to: the server and account it
    was entered for, so it is never sent to another host (or in the clear
    to the same one)."""
    return (f"{s['email_host'].lower()}|{s['email_port']}|"
            f"{s['email_security']}|{s['email_username']}")


def secret_scope(s: Dict[str, Any]) -> str:
    """What a stored push secret is bound to: the service it was entered
    for (fixing the server's address keeps it)."""
    return s["provider"]


def push_configured(s: Dict[str, Any]) -> bool:
    if not s["enabled"] or not s["push_enabled"]:
        return False
    if s["provider"] == "ntfy":
        return bool(s["server"] and s["topic"])
    if s["provider"] == "pushover":
        return bool(s["pushover_user"] and s["secret"])
    if s["provider"] == "gotify":
        return bool(s["gotify_server"] and s["secret"])
    if s["provider"] == "telegram":
        return bool(s["secret"] and s["telegram_chat"])
    if s["provider"] == "discord":
        return bool(s["secret"])
    if s["provider"] == "discord_dm":
        return bool(s["secret"] and s["discord_user"])
    return False


def email_configured(s: Dict[str, Any]) -> bool:
    return bool(s["enabled"] and s["email_enabled"] and s["email_host"]
                and s["email_from"] and recipients(s))


def webhook_configured(s: Dict[str, Any]) -> bool:
    return bool(s["enabled"] and s["webhook_enabled"] and s["webhook_url"])


def configured(s: Dict[str, Any]) -> bool:
    return push_configured(s) or email_configured(s) or webhook_configured(s)


def stored_error(s: Dict[str, Any], env: Optional[Env] = None) -> str:
    """Why the stored settings no longer pass validation (e.g. after an
    update or an edit by hand), or ""."""
    return stored_problem(s, env)["message"]


def stored_problem(s: Dict[str, Any], env: Optional[Env] = None
                   ) -> Dict[str, Any]:
    """stored_error, with the setting it's about and its value in
    pushraker.conf (for the page to point at)."""
    try:
        validate({}, s, env)
    except ConfigError as e:
        field = getattr(e, "field", "")
        # Never a token or password: the page shows the value
        value = s.get(field) if field and field in s \
            and field not in SECRET_FIELDS else None
        return {"message": str(e), "field": field,
                "value": "" if value is None else str(value)}
    return {"message": "", "field": "", "value": ""}


# ---- the printer's address and cameras --------------------------------

def camera(s: Dict[str, Any], env: Env) -> Optional[Dict[str, str]]:
    """The chosen webcam's URLs, or None when it isn't one Moonraker has."""
    return env.cameras.get(s["camera"]) if s["camera"] else None


def camera_live_url(s: Dict[str, Any], env: Env) -> str:
    """The live view for the notification's link: the webcam's stream URL,
    under the printer URL when it is a path."""
    cam = camera(s, env)
    if not cam or not s["printer_url"]:
        return ""
    stream = cam.get("stream") or ""
    if not stream:
        return ""
    if "://" in stream:
        return stream
    return printer_root(s, env) + "/" + stream.lstrip("/")


def printer_root(s: Dict[str, Any], env: Optional[Env]) -> str:
    """The Printer URL without a trailing slash, as sent."""
    return s["printer_url"].rstrip("/")


def links(s: Dict[str, Any], rid: str, env: Env) -> List[Tuple[str, str]]:
    """(title, url) buttons for a notification."""
    out = []
    if s["printer_url"]:
        out.append(("Web UI", printer_root(s, env) + "/"))
        live = camera_live_url(s, env)
        if live:
            out.append(("Live Camera", live))
    if rid in ERROR_ROWS and env.error_index_url:
        out.append(("Error Index", env.error_index_url))
    return out


def query(params: Dict[str, Any]) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())


def row_url(s: Dict[str, Any], rid: str, priority: str, env: Env) -> str:
    """Apprise URL for one notification row."""
    btns = links(s, rid, env)
    if s["provider"] == "pushover":
        # One link only: the Web UI, or the error index for faults when no
        # printer URL is set
        params: Dict[str, Any] = {"priority": PUSHOVER_PRIORITY[priority]}
        if btns:
            title, url = btns[0]
            params["url"], params["url_title"] = url, title
        device = f"/{s['pushover_device']}" if s["pushover_device"] else ""
        return (f"pover://{s['pushover_user']}@{s['secret']}{device}"
                f"?{query(params)}")
    if s["provider"] == "gotify":
        # Markdown, links in the text (the component adds them); no
        # pictures: Gotify messages can't carry attachments
        u = urlparse(s["gotify_server"])
        scheme = "gotifys" if u.scheme == "https" else "gotify"
        port = f":{u.port}" if u.port else ""
        path = (u.path or "/").rstrip("/") + "/"
        params = {"priority": GOTIFY_PRIORITY[priority], "format": "markdown"}
        return (f"{scheme}://{u.hostname}{port}{path}{s['secret']}"
                f"?{query(params)}")
    if s["provider"] == "telegram":
        # Sent as HTML, links in the text (the component adds them);
        # image=no: no Apprise icon; content=after: text under the picture
        params = {"format": "html", "image": "no", "content": "after",
                  "preview": "no", "detect": "no",
                  "silent": "yes" if priority in TELEGRAM_SILENT else "no"}
        return (f"tgram://{s['secret']}/{quote(s['telegram_chat'], safe='@-')}"
                f"?{query(params)}")
    if s["provider"] in ("discord", "discord_dm"):
        # discordhook:// and discorddm:// (pushraker.discord)
        params = {"silent": "yes" if priority in TELEGRAM_SILENT else "no"}
        if s["provider"] == "discord_dm":
            return (f"discorddm://{s['secret']}@{s['discord_user']}"
                    f"?{query(params)}")
        m = RX_DISCORD_WEBHOOK.fullmatch(s["secret"])
        if m is None:
            raise ValueError("Discord webhook URL")
        params["name"] = s["name"]
        if s["discord_mention"] and priority in DISCORD_PING:
            params["ping"] = s["discord_mention"]
        return f"discordhook://{m.group(1)}/{m.group(2)}?{query(params)}"
    u = urlparse(s["server"])
    scheme = "ntfys" if u.scheme == "https" else "ntfy"
    params = {"image": "no", "priority": priority}
    if btns:
        params["actions"] = "; ".join(
            f"view, {t}, {url}" + (", clear=true" if t == "Web UI" else "")
            for t, url in btns)
    userinfo = ""
    extra = ""
    if s["auth"] == "basic":
        userinfo = (quote(s["username"], safe="") + ":" +
                    quote(s["secret"], safe="") + "@")
    elif s["auth"] == "token":
        extra = "&token=" + quote(s["secret"], safe="")
    return (f"{scheme}://{userinfo}{u.netloc}/{s['topic']}?{query(params)}"
            f"{extra}")


def webhook_url(s: Dict[str, Any], rid: str, env: Env) -> str:
    """Apprise JSON URL for the webhook: POSTs {"title", "message", "type",
    "attachments": [image as base64], "event": <notification>, "printer":
    <name>}. The webhook's own query parameters are kept (Apprise sends
    "-name" parameters as-is)."""
    u = urlparse(s["webhook_url"])
    scheme = "jsons" if u.scheme == "https" else "json"
    # urlparse leaves userinfo percent-encoded: keep it as given
    userinfo = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    host = u.netloc.rsplit("@", 1)[-1]      # keeps IPv6 brackets and port
    own = [f"-{k}={v}" for k, v in
           (part.split("=", 1) if "=" in part else (part, "")
            for part in u.query.split("&") if part)]
    base = (f"{scheme}://{userinfo}{host}{u.path or '/'}"
            + ("?" + "&".join(own) if own else ""))
    params = [f":event={quote(rid, safe='')}",
              f":printer={quote(s['name'], safe='')}"]
    # The links, as fields (a webhook has no buttons)
    names = {"Web UI": "web_ui", "Live Camera": "live_camera",
             "Error Index": "error_index"}
    params += [f":{names[t]}={quote(url, safe='')}"
               for t, url in links(s, rid, env)]
    return base + ("&" if own else "?") + "&".join(params)


def email_url(s: Dict[str, Any]) -> str:
    """Apprise URL for email (SMTP)."""
    scheme = "mailto" if s["email_security"] == "none" else "mailtos"
    userinfo = ""
    if s["email_username"]:
        userinfo = (quote(s["email_username"], safe="") + ":" +
                    quote(s["email_password"], safe="") + "@")
    # HTML card (pushraker.email_html) with the image inline; Apprise adds
    # a plain-text version
    params = {"from": s["email_from"], "to": ",".join(recipients(s)),
              "name": s["name"], "format": "html", "inline": "yes"}
    if s["email_security"] != "none":
        params["mode"] = s["email_security"]
    return (f"{scheme}://{userinfo}{s['email_host']}:{s['email_port']}"
            f"?{query(params)}")


def available_rows(env: Env) -> List[str]:
    """The filament rows need the Spoolman integration; pre-print
    calibrations, a printer that has them."""
    return [rid for rid in ROW_IDS
            if (env.spoolman_url or rid not in SPOOLMAN_ONLY)
            and (env.preprint or rid != PREPRINT_ROW)]


def push_rows(s: Dict[str, Any], env: Env) -> List[str]:
    if not push_configured(s):
        return []
    return [rid for rid in available_rows(env) if s["rows"][rid]["enabled"]]


def email_rows(s: Dict[str, Any], env: Env) -> List[str]:
    if not email_configured(s):
        return []
    return [rid for rid in available_rows(env) if s["rows"][rid]["email"]]


def webhook_rows(s: Dict[str, Any], env: Env) -> List[str]:
    """The webhook gets every notification (the receiver picks by
    "event"), whatever the push and email choices."""
    if not webhook_configured(s):
        return []
    return available_rows(env)


def enabled_rows(s: Dict[str, Any], env: Env) -> List[str]:
    """Notifications sent to at least one destination."""
    push, email, hook = push_rows(s, env), email_rows(s, env), \
        webhook_rows(s, env)
    return [rid for rid in ROW_IDS if rid in push or rid in email
            or rid in hook]


# The component's names for the images
IMAGE_KIND = {"none": "none", "render": "render", "camera": "snapshot",
              "graphic": "spool"}


def image_kind(s: Dict[str, Any], name: str, env: Env) -> str:
    """The image a notification (by the component's name for it) sends:
    render, snapshot, spool or none."""
    key = OPTION_KEYS.get(name, name)
    if key not in TEMPLATE:
        return "none"
    t = template(s, key)
    kind = IMAGE_KIND.get(t["image"], "none")
    if kind == "snapshot" and not template_camera(s, key, env):
        return "none"
    return kind


def template_camera(s: Dict[str, Any], key: str,
                    env: Env) -> Optional[Dict[str, Any]]:
    """The camera a notification's snapshot comes from: its own, else the
    one chosen for the printer."""
    name = template(s, key)["camera"]
    if name and name in env.cameras:
        return env.cameras[name]
    return camera(s, env)


def render_options(s: Dict[str, Any], env: Env) -> Dict[str, str]:
    """The component's options, as the generated Moonraker config used to
    hold them: {} when nothing is configured. Secrets are in the URLs as
    they are (the dict never leaves the component)."""
    if not configured(s):
        return {}
    rows = enabled_rows(s, env)
    if not rows:
        return {}
    cam = camera(s, env)
    push, email, hook = push_rows(s, env), email_rows(s, env), \
        webhook_rows(s, env)
    out: Dict[str, str] = {"name": s["name"]}
    if cam and cam.get("snapshot"):
        out["snapshot_url"] = cam["snapshot"]
    out["layers_complete"] = str(s["layers_complete"])
    out["complete_objects"] = s["complete_objects"]
    out["first_layer_objects"] = s["first_layer_objects"]
    if "filament_change" in rows:
        out["filament_change_events"] = s["filament_change_events"]
    if "driver_hot" in rows:
        out["driver_temp"] = str(s["driver_temp"])
    if "status_report" in rows:
        out["report_interval"] = str(s["report_interval"])
    tz = time_zone(s)
    if tz:
        out["timezone"] = tz
    if s["time_left"]:
        out["time_left"] = s["time_left"]
    if any(r in rows for r in SPOOLMAN_ROWS) and env.spoolman_url:
        out["spoolman_url"] = env.spoolman_url
        out["filament_low_percent"] = str(s["filament_low_percent"])
        out["filament_low_grams"] = str(s["filament_low_grams"])
        out["filament_unknown"] = "true" if s["filament_unknown"] else "false"
    # Links in the text: email and Telegram have no buttons
    text_links = email or (push and s["provider"] in (
        "gotify", "telegram", "discord", "discord_dm"))
    if text_links and s["printer_url"]:
        out["web_ui_url"] = printer_root(s, env) + "/"
        live = camera_live_url(s, env)
        if live:
            out["camera_url"] = live
    if env.error_index_url:
        out["error_index_url"] = env.error_index_url
    for key in TEMPLATE_KEYS:
        cam = template_camera(s, key, env)
        if cam and cam.get("snapshot") and template(s, key)["camera"]:
            out[f"snapshot_url_{key}"] = cam["snapshot"]
    # Custom notifications: like a row
    for c in s.get("custom") or []:
        rid = custom_rid(c["name"])
        kind = IMAGE_KIND[c["image"]]
        if kind == "snapshot" and not (
                env.cameras.get(c["camera"]) or camera(s, env) or {}
        ).get("snapshot"):
            kind = "none"
        if c["camera"] and (env.cameras.get(c["camera"]) or {}).get(
                "snapshot"):
            out[f"snapshot_url_{rid}"] = env.cameras[c["camera"]]["snapshot"]
        if c["enabled"] and push_configured(s):
            pkind = "none" if s["provider"] == "gotify" else kind
            out[rid] = f"{pkind}, {row_url(s, rid, c['priority'], env)}"
            for p in PRIORITIES:
                if p != c["priority"]:
                    out[f"{rid}@{p}"] = row_url(s, rid, p, env)
        if c["email"] and email_configured(s):
            out[f"{rid}_email"] = f"{kind}, {email_url(s)}"
        if webhook_configured(s):
            out[f"{rid}_webhook"] = f"{kind}, {webhook_url(s, rid, env)}"
    for rid in rows:
        kind = image_kind(s, rid, env)
        if rid == "progress":
            # The component has a row per mark
            for m in progress_marks(s):
                name = PROGRESS_ROWS[m]
                if rid in push:
                    pkind = "none" if s["provider"] == "gotify" else kind
                    out[name] = f"{pkind}, " \
                        f"{row_url(s, rid, s['rows'][rid]['priority'], env)}"
                    for pr in PRIORITIES:
                        if pr != s["rows"][rid]["priority"]:
                            out[f"{name}@{pr}"] = row_url(s, rid, pr, env)
                if rid in email:
                    out[f"{name}_email"] = f"{kind}, {email_url(s)}"
                if rid in hook:
                    out[f"{name}_webhook"] = \
                        f"{kind}, {webhook_url(s, name, env)}"
            continue
        if rid == "macro":
            # One set of destinations per priority: a RESPOND PREFIX=notify
            # line may name its own; the row's priority is the default
            out["macro_priority"] = s["rows"][rid]["priority"]
            for p in PRIORITIES:
                name = f"macro_{p}"
                if rid in push:
                    pkind = "none" if s["provider"] == "gotify" else kind
                    out[name] = f"{pkind}, {row_url(s, rid, p, env)}"
                if rid in email:
                    out[f"{name}_email"] = f"{kind}, {email_url(s)}"
                if rid in hook:
                    out[f"{name}_webhook"] = \
                        f"{kind}, {webhook_url(s, 'macro', env)}"
            continue
        kind = image_kind(s, rid, env)
        if rid in push:
            prio = s["rows"][rid]["priority"]
            pkind = "none" if s["provider"] == "gotify" else kind
            out[rid] = f"{pkind}, {row_url(s, rid, prio, env)}"
            # A template may set its own priority ({% set priority =
            # "max" %}): the row's push address at each other priority
            for p in PRIORITIES:
                if p != prio:
                    out[f"{rid}@{p}"] = row_url(s, rid, p, env)
        if rid in email:
            out[f"{rid}_email"] = f"{kind}, {email_url(s)}"
        if rid in hook:
            out[f"{rid}_webhook"] = f"{kind}, {webhook_url(s, rid, env)}"
    return out


def time_zone(s: Dict[str, Any]) -> str:
    """The time zone ETAs are given in."""
    return s["browser_timezone"] if s["timezone"] in ("", "browser") \
        else s["timezone"]


def hook_values(s: Dict[str, Any], env: Env) -> Dict[str, int]:
    """What the Klipper print hooks do, per event: 0 nothing, 1 report, 2
    lift the toolhead for the snapshot and report (SET_GCODE_VARIABLE on
    _NTFY_HOOKS; see klipper/pushraker.cfg)."""
    rows = enabled_rows(s, env) if configured(s) else []

    def value(rid: Optional[str]) -> int:
        if not rid or rid not in rows:
            return 0
        return 2 if image_kind(s, rid, env) == "snapshot" else 1
    return {"complete": value("complete"),
            "cancelled": value("cancelled"),
            # A cancel after a fault: the error row, else the cancel row
            "fault": value("error" if "error" in rows else "cancelled")}


# ---- lookups the page asks for at save time ------------------------------

def telegram_find_chat(s: Dict[str, Any]) -> str:
    """Fill in an empty Telegram chat ID from the bot's recent messages:
    the chat of the latest message sent to the bot (a private chat, or a
    group the bot was added to). Telegram keeps them for 24 hours. Returns
    a line for the user, "" when nothing was looked up."""
    if not s["push_enabled"] or s["provider"] != "telegram" or \
            s["telegram_chat"] or not s["secret"]:
        return ""
    url = f"{TELEGRAM_API}{s['secret']}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            raise ConfigError("Telegram doesn't accept this bot token; copy "
                              "it again from @BotFather")
        raise ConfigError(f"Could not reach Telegram ({e})")
    except Exception as e:
        raise ConfigError(f"Could not reach Telegram ({e})")
    chat = None
    for upd in data.get("result") or []:
        msg = (upd.get("message") or upd.get("my_chat_member")
               or upd.get("channel_post") or {})
        chat = msg.get("chat") or chat
    if not chat or "id" not in chat:
        raise ConfigError("Open your bot in Telegram and press Start (or "
                          "send it any message), then try again. To use a "
                          "group, add the bot to it and send a message there.")
    s["telegram_chat"] = str(chat["id"])
    name = chat.get("title") or " ".join(
        x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
        or chat.get("username") or ""
    return f"Telegram chat found: {name} ({s['telegram_chat']})"


def discord_find_user(s: Dict[str, Any]) -> str:
    """Discord DM: check the bot token and send to the bot's owner (the
    person who made it in the Developer Portal) unless a user ID is set.
    The bot must share a server with that user."""
    if not s["push_enabled"] or s["provider"] != "discord_dm" or \
            not s["secret"]:
        return ""
    from . import discord as d
    app, why = d.bot_info(s["secret"])
    if app is None:
        raise ConfigError(why + " (Developer Portal > your app > Bot > "
                          "Reset Token, then copy it)")
    line = ""
    if not s["discord_user"]:
        s["discord_user"] = d.owner_id(app)
        if not s["discord_user"]:
            raise ConfigError("Could not find who owns the bot; enter your "
                              "Discord user ID")
        line = (f"Discord: messages go to the bot's owner "
                f"({(app.get('owner') or {}).get('username') or s['discord_user']})")
    if d.bot_servers(s["secret"]) == 0:
        raise ConfigError("Add the bot to a server you are in (Discord only "
                          "lets a bot message people it shares a server "
                          f"with): {d.invite_url(app['id'])}")
    return line


# ---- what the page sees ---------------------------------------------------

def summary(s: Dict[str, Any], env: Optional[Env] = None) -> str:
    """One line: what's on."""
    if stored_error(s, env):
        return "Needs attention"
    if not s["enabled"]:
        return "Notifications disabled"
    on = []
    if push_configured(s):
        on.append({"discord_dm": "Discord"}.get(
            s["provider"], s["provider"].capitalize()
            if s["provider"] != "ntfy" else "ntfy"))
    if email_configured(s):
        on.append("email")
    if webhook_configured(s):
        on.append("webhook")
    return "Enabled: " + ", ".join(on) if on else "Enabled (not set up)"


def public_view(s: Dict[str, Any], env: Env, busy: bool,
                version: str) -> Dict[str, Any]:
    """The settings for the page: secrets blanked (has_<secret> says
    whether one is stored), plus what the page needs to know about the
    printer."""
    out = json.loads(json.dumps(s))
    for key in SECRET_FIELDS:
        out[f"has_{key}"] = bool(out[key])
        out[key] = ""
    out.pop("secret_for", None)
    out.pop("email_password_for", None)
    out["meta"] = {
        "version": version,
        "rows": [{"id": r[0], "label": r[1], "icon": r[2],
                  "image": r[5], "group": r[6],
                  "available": r[0] not in env.rows_unavailable,
                  # Left off the page only when the printer can't produce
                  # it and it is off: one turned on in pushraker.conf shows
                  "hidden": r[0] in env.rows_unavailable
                  and not s["rows"][r[0]]["enabled"]
                  and not s["rows"][r[0]]["email"]}
                 for r in ROWS],
        "email_presets": EMAIL_PRESETS,
        "spoolman": bool(env.spoolman_url),
        "preprint": env.preprint,
        "progress_marks": list(PROGRESS_MARKS),
        "time_methods": env.time_methods,
        "time_default": env.time_default,
        "templates": [{"key": t[0], "row": t[1], "label": t[2],
                       "images": list(t[4]),
                       "default": template_default(t[0])}
                      for t in TEMPLATES],
        "title_max": TITLE_MAX, "contents_max": CONTENTS_MAX,
        "custom_default": CUSTOM_DEFAULT, "custom_max": CUSTOM_MAX,
        "priorities": PRIORITIES,
        "providers": PROVIDERS,
        "cameras": {name: {"snapshot": bool(c.get("snapshot")),
                           "stream": bool(c.get("stream"))}
                    for name, c in env.cameras.items()},
        "busy": busy,
        "default_name": env.name,
        "stored_error": stored_error(s, env),
        "stored_problem": stored_problem(s, env),
        "summary": summary(s, env),
    }
    return out
