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
from typing import Any, Dict, List, Optional, Sequence, Tuple
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
    # Filament loaded into or unloaded from a toolhead, outside a print job
    ("filament_change", "Filament loaded / unloaded", "🧵", False, "low",
     "spool", "print", False),
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
    ("slot_empty", "ACE slot ran empty", "🧵", True, "default", "spool",
     "refill", False),
]
ROW_IDS = [r[0] for r in ROWS]
ROW = {r[0]: r for r in ROWS}
# Print-by-object: "Print complete" for each object is sent as its own
# notification (so a webhook sees event=object_complete), with the Print
# complete row's settings. It isn't a row of its own in the form.
OBJECT_ROW = "object_complete"
# A pause the print job's file asks for is sent with the Paused row's
# destinations at the highest priority: the file wants something from you
FILE_PAUSE_ROW = "file_pause"
# Pre-print calibrations done (printers that have them): sent with the
# Print started row's settings, when its checkbox is on
PREPRINT_ROW = "preprint_done"
# The Progress row's marks, and the component's row for each
PROGRESS_MARKS = (25, 50, 75)
PROGRESS_ROWS = {m: f"progress_{m}" for m in PROGRESS_MARKS}
# Notification data (templating.py): a row adds the default data, its own
# text, or nothing
DATA_MODES = ("default", "custom", "none")
DATA_MAX = 1500
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
                 "camera", "name", "timezone", "first_layer_objects",
                 "filament_change_events",
                 "complete_objects",
                 "email_host",
                 "email_security", "email_username", "email_from",
                 "email_to", "progress_marks", "time_left")
# Free text with line breaks (stored with \n escaped)
TEXT_FIELDS = ("data_default",)
SECRET_FIELDS = ("secret", "email_password", "webhook_url")
INT_FIELDS = ("layers_complete", "email_port", "filament_low_percent",
              "filament_low_grams", "driver_temp", "report_interval")
BOOL_FIELDS = ("enabled", "push_enabled", "email_enabled", "webhook_enabled",
               "filament_unknown", "preprint_done")
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
    pass


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
        "timezone": "",
        "layers_complete": 2,
        # Print-by-object: "layer N complete" and "Print complete" for
        # each object, or once per print job
        "first_layer_objects": "each",
        "filament_change_events": "loaded,unloaded",
        "complete_objects": "each",
        "filament_low_percent": 10,
        "filament_low_grams": 50,
        "filament_unknown": True,
        # Under Print started: also notify when the pre-print calibrations
        # chosen for the job are done
        "preprint_done": True,
        # The Progress row: which of 25, 50, 75 % it reports
        "progress_marks": "25,50,75",
        # Time-left method (component.TIME_METHODS and the printer's own);
        # "" for the printer's default
        "time_left": "",
        # Data added to the end of every notification whose DATA is
        # Default ({object.field} tokens, see templating.py); "" for none
        "data_default": "",
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
                        "image": r[5] is not None, "email": r[7],
                        "data_mode": "default", "data": ""}
                 for r in ROWS},
    }


def escape_text(text: str) -> str:
    """Line breaks on one INI line."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def unescape_text(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n"
                  else m.group(1), text)


SECTION = "pushraker"
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
    lines.append("# Data added to notifications: {object.field|format} tokens, "
                 "\\n for a line break")
    for key in TEXT_FIELDS:
        lines.append(f"{key}: {escape_text(s[key])}")
    lines.append("# <notification>: on|off (push), <priority>, image|no_image, "
                 "email|no_email")
    lines.append("# <notification>_data: default | none | custom, <text>")
    for rid in ROW_IDS:
        r = s["rows"][rid]
        lines.append(f"{rid}: {'on' if r['enabled'] else 'off'}, "
                     f"{r['priority']}, "
                     f"{'image' if r['image'] else 'no_image'}, "
                     f"{'email' if r['email'] else 'no_email'}")
        mode = r.get("data_mode", "default")
        if mode != "default" or r.get("data"):
            lines.append(f"{rid}_data: {mode}" + (
                f", {escape_text(r['data'])}" if mode == "custom" else ""))
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
    for key in TEXT_FIELDS:
        if key in sec:
            s[key] = unescape_text(sec.get(key, "").strip())
    for rid in ROW_IDS:
        raw = sec.get(rid)
        if raw:
            _parse_row(s["rows"][rid], raw)
        raw = sec.get(f"{rid}_data")
        if raw:
            mode, _, text = raw.partition(",")
            mode = mode.strip().lower()
            if mode in DATA_MODES:
                s["rows"][rid]["data_mode"] = mode
                s["rows"][rid]["data"] = unescape_text(text.strip()) \
                    if mode == "custom" else ""
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
            image=first["image"], email=first["email"])
    s["camera"] = LEGACY_CAMERAS.get(s["camera"], s["camera"])
    return s


def _parse_row(row: Dict[str, Any], raw: str) -> Dict[str, Any]:
    parts = [p.strip().lower() for p in raw.split(",")]
    row["enabled"] = parts[0] == "on"
    if len(parts) > 1 and parts[1] in PRIORITIES:
        row["priority"] = parts[1]
    if len(parts) > 2:
        row["image"] = parts[2] == "image"
    if len(parts) > 3:
        row["email"] = parts[3] == "email"
    return row


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
        """Secrets first: if their file can't be written, the settings
        (which hold none) are untouched."""
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
        self._write(self.secrets_path, buf.getvalue() if secrets else "",
                    0o600)
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        self._write(self.settings_path, "".join(section_lines(s)), 0o644)
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
    for key in TEXT_FIELDS:
        if key in data:
            s[key] = clean_text(data[key])
    for key in SECRET_FIELDS:
        if data.get(key):
            s[key] = str(data[key])
    for key, lo, hi in (("filament_low_percent", 0, 90),
                        ("filament_low_grams", 0, 5000)):
        if key in data:
            try:
                s[key] = int(data[key] or 0)
            except (TypeError, ValueError):
                raise ConfigError("Filament alert values must be numbers")
        if not lo <= s[key] <= hi:
            raise ConfigError("Filament alert: 0-90 % and 0-5000 g")
    if "email_port" in data:
        try:
            s["email_port"] = int(data["email_port"])
        except (TypeError, ValueError):
            raise ConfigError("Email port must be a number")
    if "layers_complete" in data:
        try:
            s["layers_complete"] = int(data["layers_complete"])
        except (TypeError, ValueError):
            raise ConfigError("Layer must be 1, 2 or 3")
    if "driver_temp" in data:
        try:
            s["driver_temp"] = int(data["driver_temp"])
        except (TypeError, ValueError):
            raise ConfigError("Driver temperature must be a number")
    if not 40 <= s["driver_temp"] <= 150:
        raise ConfigError("Driver temperature alert: 40 to 150 °C")
    if "report_interval" in data:
        try:
            s["report_interval"] = int(data["report_interval"])
        except (TypeError, ValueError):
            raise ConfigError("Status report interval must be a number")
    if not 5 <= s["report_interval"] <= 720:
        raise ConfigError("Status report: every 5 to 720 minutes")
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
        r["image"] = bool(row.get("image", r["image"]))
        r["email"] = bool(row.get("email", r["email"]))
        if row.get("data_mode") in DATA_MODES:
            r["data_mode"] = row["data_mode"]
        if "data" in row:
            r["data"] = clean_text(row["data"])
        if r.get("data_mode", "default") != "custom":
            r["data"] = ""
    for rid in ROW_IDS:
        if ROW[rid][5] is None:
            s["rows"][rid]["image"] = False
    if s["time_left"] == "first_layer":       # 0.3.1's name
        s["time_left"] = "slicer_first_layer"
    from .templating import placeholders
    for rid, text in [("default", s["data_default"])] + [
            (rid, s["rows"][rid].get("data", "")) for rid in ROW_IDS]:
        left = placeholders(text)
        if left:
            what = "Default data" if rid == "default" else ROW[rid][1]
            raise ConfigError(f"{what}: replace # in {left[0]} with a "
                              "filament number")
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
    # webhook). A stored one is only reused for the service, server and
    # account it was entered for: switching services means entering the
    # new one, and a saved token is never sent to another host.
    scope = secret_scope(s)
    if s["secret"] and s.get("secret_for", "") not in ("", scope):
        s["secret"] = ""
        if s["push_enabled"]:
            raise ConfigError(
                "The saved token or password was entered for another "
                "service or server. Enter the one for this one.")
    s["secret_for"] = scope if s["secret"] else ""
    if s["provider"] not in PROVIDERS:
        raise ConfigError("Invalid notification service")
    if s["auth"] not in AUTH_MODES:
        raise ConfigError("Invalid authentication mode")
    s["camera"] = LEGACY_CAMERAS.get(s["camera"], s["camera"])
    if not RX_CAMERA.fullmatch(s["camera"]):
        raise ConfigError("Invalid camera")
    if s["layers_complete"] not in (1, 2, 3):
        raise ConfigError("Layer must be 1, 2 or 3")
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
    if not RX_TIMEZONE.fullmatch(s["timezone"]):
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


def clean_text(value: Any) -> str:
    """Notification data as typed: line breaks kept, other control
    characters dropped, capped."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]*\n", "\n", text)
    text = "".join(c for c in text if c == "\n" or not RX_CONTROL.match(c))
    if len(text) > DATA_MAX:
        raise ConfigError(f"Notification data: up to {DATA_MAX} characters")
    return text.strip()


def progress_marks(s: Dict[str, Any]) -> List[int]:
    return [m for m in PROGRESS_MARKS if str(m) in s["progress_marks"].split(",")]


def row_data(s: Dict[str, Any], rid: str) -> str:
    """The data text a row adds: the default, its own, or none."""
    r = s["rows"].get(rid) or {}
    mode = r.get("data_mode", "default")
    if mode == "custom":
        return r.get("data", "")
    if mode == "none":
        return ""
    return s.get("data_default", "")


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
    """What a stored push secret is bound to."""
    if s["provider"] == "ntfy":
        return f"ntfy|{s['auth']}|{s['server'].lower()}|{s['username']}"
    if s["provider"] == "gotify":
        return f"gotify|{s['gotify_server'].lower().rstrip('/')}"
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
    update), or ""."""
    try:
        validate({}, s, env)
    except ConfigError as e:
        return str(e)
    return ""


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
    """The filament rows need the Spoolman integration."""
    if env.spoolman_url:
        return list(ROW_IDS)
    return [rid for rid in ROW_IDS if rid not in SPOOLMAN_ONLY]


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


def image_kind(s: Dict[str, Any], rid: str, env: Env) -> str:
    kind = ROW[rid][5]
    if not s["rows"][rid]["image"] or kind is None:
        return "none"
    if kind == "snapshot" and not camera(s, env):
        return "none"
    return kind


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
    out["first_layer_objects"] = s["first_layer_objects"]
    if "filament_change" in rows:
        out["filament_change_events"] = s["filament_change_events"]
    if "driver_hot" in rows:
        out["driver_temp"] = str(s["driver_temp"])
    if "status_report" in rows:
        out["report_interval"] = str(s["report_interval"])
    if s["timezone"]:
        out["timezone"] = s["timezone"]
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
    for rid in rows:
        text = row_data(s, rid)
        if text:
            out[f"data_{rid}"] = text
        kind = image_kind(s, rid, env)
        if rid == "progress":
            # The component has a row per mark
            for m in progress_marks(s):
                name = PROGRESS_ROWS[m]
                if rid in push:
                    pkind = "none" if s["provider"] == "gotify" else kind
                    out[name] = f"{pkind}, " \
                        f"{row_url(s, rid, s['rows'][rid]['priority'], env)}"
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
        # The object-complete notification follows the Print complete row
        for name in ([rid, OBJECT_ROW] if rid == "complete"
                     and s["complete_objects"] == "each" else
                     [rid, FILE_PAUSE_ROW] if rid == "paused" else
                     [rid, PREPRINT_ROW] if rid == "started"
                     and s["preprint_done"] and env.preprint else [rid]):
            if rid in push:
                url = row_url(s, rid, "max" if name == FILE_PAUSE_ROW
                              else s["rows"][rid]["priority"], env)
                pkind = "none" if s["provider"] == "gotify" else kind
                out[name] = f"{pkind}, {url}"
            if rid in email:
                out[f"{name}_email"] = f"{kind}, {email_url(s)}"
            if rid in hook:
                out[f"{name}_webhook"] = f"{kind}, {webhook_url(s, name, env)}"
    return out


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
        "data_modes": list(DATA_MODES),
        "data_max": DATA_MAX,
        "priorities": PRIORITIES,
        "providers": PROVIDERS,
        "cameras": {name: {"snapshot": bool(c.get("snapshot")),
                           "stream": bool(c.get("stream"))}
                    for name, c in env.cameras.items()},
        "busy": busy,
        "default_name": env.name,
        "stored_error": stored_error(s, env),
        "summary": summary(s, env),
    }
    return out
