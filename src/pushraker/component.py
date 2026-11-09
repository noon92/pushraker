# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

# Push Notifications — print, progress and fault notifications with images,
# sent through Apprise (ntfy, Pushover, Telegram, Discord, a webhook, email).
#
# Sources:
#  * Moonraker job events: started, paused, resumed, error (and complete /
#    cancelled as a fallback when the print hooks did not report them).
#  * The Klipper print hooks (`_PRINT_END_NTFY`, `_CANCEL_PRINT_NTFY`), via
#    the `pushraker` remote method (`push_notification` in 0.1), so complete/cancelled snapshots
#    are taken before the printer parks.
#  * Snapmaker's exception manager: coded faults and warnings, including
#    those that never reach print_stats (Klipper shutdowns, idle faults).
#  * print_stats layer info and file progress: first layer and milestones.
#  * Power-loss recovery data after boot.
#
# Images are fetched before sending; when the camera or the thumbnail is not
# available, the notification is sent without the image. A send that the
# server answers with an error (or that can't connect) is retried after 6, 10
# and 30 s; the last retry is without the image. Fault codes use Snapmaker's
# format LLLL-IIII-XXXX-CCCC (level-id-index-code).
#
# Settings normally come from pushraker.conf (the settings page writes it).
# The older form, still read: the section itself carries the notifications.
# Each notification can go to a push service (`<notification>: ...`) and/or email
# (`<notification>_email: ...`) and/or a webhook (`<notification>_webhook:
# ...`), each `<render|snapshot|spool|none>, <apprise url>`;
# notifications that are not listed are disabled. Each destination is sent
# and retried on its own. Emails, Telegram and Discord get the links (Web UI, Live
# Camera, Error Index) in the text, since they have no buttons:
#
#   [pushraker]
#   settings: options
#   name: U1
#   snapshot_url: http://127.0.0.1/webcam/snapshot.jpg
#   layers_complete: 2               # first-layer row: after layer 1, 2 or 3
#   timezone: Asia/Jerusalem         # for ETA clock times (printer runs UTC)
#   web_ui_url: http://u1/           # links in the text (optional)
#   camera_url: http://u1/webcam/webrtc
#   started: render, ntfys://ntfy.example.com/u1?priority=default
#   error: snapshot, ntfys://ntfy.example.com/u1?priority=max
#   error_email: snapshot, mailtos://user:pass@smtp.example.com:587?...
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import datetime
import html
import http
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.request
from typing import (
    TYPE_CHECKING, Any, Awaitable, Dict, List, NamedTuple, Optional, Sequence, Set,
    Tuple
)

import apprise

try:
    from . import images
except Exception as e:      # no Pillow: Spoolman notifications without images
    logging.warning("[pushraker] spool images unavailable: %s", e)
    images = None  # type: ignore

try:
    from . import email_html
except Exception as e:
    logging.warning("[pushraker] HTML email unavailable: %s", e)
    email_html = None  # type: ignore

try:
    # Registers the discordhook:// and discorddm:// Apprise plugins
    from . import discord
except Exception as e:
    logging.warning("[pushraker] Discord unavailable: %s", e)
    discord = None  # type: ignore

from moonraker.common import JobEvent, KlippyState
from . import settings as cfgmod
from . import printers
from . import templating as tp
from . import __version__

if TYPE_CHECKING:
    from moonraker.confighelper import ConfigHelper
    from moonraker.components.klippy_apis import KlippyAPI as APIComp

# The printer's own fault event (see printers.py); listened for from the
# start, it simply never fires on a printer without one
EXCEPTION_EVENTS = printers.EXCEPTION_EVENTS
REMOTE_METHOD = "pushraker"
# The 0.1 name, still answered (a Klipper macro file from 0.1 calls it)
OLD_REMOTE_METHOD = "push_notification"
MILESTONES = (25, 50, 75)
ROW_IDS = (
    "started", "preprint_done", "layer_started", "first_layer", "progress_25", "progress_50", "progress_75",
    "status_report",
    "paused", "file_pause", "resumed", "object_complete", "complete", "cancelled",
    "calibration",
    "chamber_waiting", "chamber_reached", "chamber_fault",
    "filament_change",
    "macro_min", "macro_low", "macro_default", "macro_high", "macro_max",
    "error", "warning", "shutdown", "power_loss", "driver_hot",
    "filament_short", "filament_low", "spool_switch", "slot_empty",
)
SPOOLMAN_ROWS = ("filament_short", "filament_low", "spool_switch")
# Notification data (templating.py) is set per settings row; the
# component's rows that share one
DATA_ALIAS = {"preprint_done": "started", "file_pause": "paused",
              "object_complete": "complete",
              **{f"progress_{m}": "progress" for m in MILESTONES},
              **{f"macro_{p}": "macro" for p in
                 ("min", "low", "default", "high", "max")}}
DATA_QUERY_TIMEOUT = 5.
# Time-left methods, id -> (label, help); see _left_seconds
TIME_METHODS = {
    "blend": ("Blend", "The slicer's estimate until 30 % of the file, then "
              "a weighted mix of the slicer's estimate, filament used and "
              "the pace so far, leaning on the pace as the print goes on "
              "(KlipperScreen's method). The most accurate on tested prints."),
    "slicer": ("Slicer", "The slicer's estimated print time minus the time "
               "printed so far (Fluidd's and Mainsail's \"slicer\" "
               "estimate). Early by the length of the printer's start "
               "routine, which the slicer doesn't know about."),
    "slicer_first_layer": ("Slicer from first layer", "The slicer's estimate "
                    "counted from when layer 1 began, so the start routine "
                    "(heating, bed scan, calibrations) doesn't count "
                    "against it."),
    "file": ("File", "Time printed so far divided by file progress, minus "
             "the time printed: the pace so far, extrapolated (Fluidd's "
             "and Mainsail's \"file\" estimate). Wild early in a print, "
             "good near the end."),
    "filament": ("Filament", "The same extrapolation by filament used "
                 "against the file's filament total."),
    "m73": ("M73", "The slicer's own time-left markers in the file (M73 R, "
            "written by PrusaSlicer, OrcaSlicer and their forks), at the "
            "current position. A file without them uses Blend."),
}
# (see _left_seconds); the printer harness may add its
# own and picks the default. KlipperScreen's blend hands over from the
# slicer's estimate to the measured pace from this far into the file
BLEND_ABOVE = 0.3
RX_M73 = re.compile(rb"^M73\s+(?:[^;\r\n]*?\s)?R(\d+(?:\.\d+)?)", re.M | re.I)
# The preview's sample print job (when none is running): a Benchy, with as
# many filaments and loaded toolheads as a token asks for
SAMPLE_FILAMENTS = [
    # name, material, vendor, colour, nozzle °C, bed °C
    ("PLA Basic White", "PLA", "Generic", "#FFFFFF", 220, 60),
    ("PETG HF Black", "PETG", "Generic", "#000000", 250, 80),
    ("TPU 95A Orange", "TPU", "Generic", "#F98C36", 230, 40),
    ("PLA Matte Grey", "PLA", "Generic", "#808080", 215, 55),
]
SAMPLE_LEFT = 41 * 60          # the sample's time remaining, seconds
# Klipper's toolheads (extruder, extruder1, ...) and the fields pushraker
# adds to them: the filament loaded, the MMU slot feeding it
RX_EXTRUDER = re.compile(r"extruder(\d*)")
EXTRUDER_ADDED = ("filament", "slot")
TITLES = {
    "started": "▶️ {name} started",
    "preprint_done": "📐 {name} pre-print calibrations complete",
    "layer_started": "🎬 {name} first layer started",
    "first_layer": "🔍 {name} {ordinal} layer complete",
    "progress": "⏳ {name} {pct}%",
    "status_report": "⏱️ {name} status report",
    "paused": "⏸️ {name} paused",
    "resumed": "▶️ {name} resumed",
    "recovered": "▶️ {name} resumed after power loss",
    "object_complete": "📦 {name} object complete",
    "complete": "✅ {name} complete",
    "cancelled": "🚫 {name} cancelled",
    "calibration": "📐 {name} {what} complete",
    "macro": "📣 {name} {title}",
    "calibration_failed": "📐 {name} {what} failed",
    "intervention": "👋 {name} user intervention needed",
    "file_pause": "👋 {name} paused by the print job",
    "chamber_waiting": "🌡️ {name} waiting for chamber temperature",
    "chamber_reached": "🌡️ {name} chamber temperature reached",
    "chamber_timeout": "⚠️ {name} timed out waiting for chamber "
                       "temperature, printing anyway",
    "chamber_stalled": "⚠️ {name} chamber not heating",
    "chamber_fault": "⚠️ {name} chamber heater fault",
    "chamber_disconnected": "⚠️ {name} chamber heater disconnected",
    "error": "⚠️ {name} Error: {kind}",
    "warning": "⚠️ {name} Warning",
    "shutdown": "🛑 {name} Error: shutdown",
    "power_loss": "⚡ {name} power loss",
    "driver_hot": "🌡️ {name} driver running hot",
    "filament_loaded": "🧵 {name} filament loaded",
    "filament_unloaded": "🧵 {name} filament unloaded",
    "filament_short": "🧵 {name} not enough filament",
    "filament_close": "🧵 {name} filament may be insufficient",
    "filament_unknown": "🧵 {name} filament amount unknown",
    "filament_problems": "🧵 {name} filament problems on {n} toolheads",
    "filament_low": "🧵 {name} spool running low",
    "filament_lows": "🧵 {name} spools running low on {n} toolheads",
    "spool_switch": "🔄 {name} switched to backup spool",
    "slot_empty": "🧵 {name} ACE slot ran empty",
}
IMAGE_KINDS = ("render", "snapshot", "spool", "none")
# machine_state_manager.main_state values of the manual calibrations run
# from the touchscreen (klippy/extras/machine_state_manager.py); the
# calibration is complete when the state goes back to IDLE (0; PRINTING
# is 1, seen in the Moonraker log as "machine state: {...}")
# (the calibrations and their states are the printer's: printers.py)
# action_code values (same file) of the steps that wait for a person:
# MANUAL_CLEAN_EXTRUDER..3 (brush the nozzle, then tap the screen)
# (manual_clean: printers.py)
# ...and a calibration sitting at action IDLE (0) this long, with Klipper
# idle too (idle_timeout "Ready": no moves, no heat-up running), is the
# screen waiting for a tap (plate out / plate back in). Picking up and
# heating a toolhead also runs at action IDLE, for longer than this, but
# Klipper is "Printing" then, which restarts the count.
INTERVENTION_HOLD = 15.
# Filament loaded / unloaded: print_task_config.filament_exist per toolhead
# (the toolhead's sensor, or the feeder's). It can flicker while filament
# is fed, so a change is reported once it has held this long. Not during
# a print job: runouts and refills have their own notifications.
FILAMENT_CHANGE_SETTLE = 3.
FAULT_ROWS = ("error", "warning", "shutdown")
# Chamber temperature, whatever heats the chamber: a print job "waits" when,
# before its first layer, there is a chamber target and the chamber is below
# it. Targets come from any heater_generic (DragonBreath, Panda Breath,
# others; M141/M191) or Snapmaker's own chamber wait (WAIT_CHAMBER_TEMP,
# which only runs with Snapmaker's purifier connected and shows as action
# PREHEAT_CHAMBER). M191 releases a few degrees short of the target
# (DragonBreath: 5 °C), so within this of it counts as reached.
CHAMBER_ROWS = ("chamber_waiting", "chamber_reached", "chamber_fault")
CHAMBER_TOLERANCE = 5.
# Waiting with no rise of this much for this long: "not heating"
CHAMBER_STALL_RISE = 1.
CHAMBER_STALL_TIME = 600.
# A chamber wait counts once it has lasted this long: the printer's own
# wait can end within seconds (it asks for 45 °C in keep-warm mode and
# gives up at once with the lid off), and that is not worth two
# notifications. Nothing is sent about a wait that ended sooner.
CHAMBER_WAIT_MIN = 30.
# (the chamber sensor's name and the preheat action: printers.py)
DRAGONBREATH = "dragonbreath"
# The settings file is re-read this often for edits made by hand
SETTINGS_POLL = 20.
# The Klipper macro holding what the print hooks do (klipper/pushraker.cfg)
HOOKS_MACRO = "_NTFY_HOOKS"
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}
SNAPSHOT_TIMEOUT = 5.
SNAPSHOT_MAX = 5 * 1024 * 1024
# A pause is reported when it starts; the head moves away from the part
# afterwards, so the snapshot waits (the bed doesn't lower on a pause).
PAUSE_SNAPSHOT_DELAY = 3.
# A runout pause on a toolhead the printer can refill from a backup spool:
# the printer swaps toolheads within ~10 s and resumes by itself, so the
# error is held this long and dropped if the refill happens (the "Switched
# to backup spool" notification says it instead)
REFILL_WAIT = 25.
# multiACE (klipper/extras/ace.py): with Quad Replenish on, a runout on a
# head fed by an ACE is reloaded from another slot of the ACE (a full load
# takes ~90 s) and the print job resumes by itself: the error is held
# this long instead. Status fields read from the `ace` object; indices in
# it are 0-based, shown 1-based like multiACE's own messages.
ACE_OBJECT = "ace"
ACE_FIELDS = ["head_source", "aces", "mode", "head_ace",
              "quad_replenish", "quad_first", "swap_in_progress"]
ACE_REFILL_WAIT = 180.
MACRO_PREFIX = "notify "
MACRO_SEP = " || "
MACRO_LEVELS = ("min", "low", "default", "high", "max")
# (runout_exception_id: printers.py)
# Before each attempt, wait this long at most for the printer to have a
# network (Wi-Fi is often not up yet right after boot). Waiting doesn't
# count as an attempt; nothing has been sent, so it can't cause doubles.
NETWORK_WAIT = 600.
NETWORK_POLL = 5.
# Spoolman notifications: how often to read the spools' remaining weight
# during a print. A toolhead is "may be insufficient" (orange) when it has
# enough by less than max(20 g, 5% of what it needs).
FILAMENT_POLL = 60.
FILAMENT_CLOSE = 0.05
FILAMENT_CLOSE_G = 20.
RANK = {"green": 0, "orange": 1, "red": 2}
# Message length limits: (counted in characters?, limit). ntfy's is 4096
# bytes; Pushover's 1024 characters. Longer messages are split between
# toolheads, and names shortened with "…" only if one toolhead won't fit.
# Telegram's is 4096 characters after HTML escaping and the links.
# Discord's embed text is 4096 characters, after Markdown escaping and links.
BODY_LIMITS = {"pushover": (True, 1024), "ntfy": (False, 4000),
               "telegram": (True, 3500), "discord": (True, 3500)}
# Discord's embed colour (Apprise notification type) for each notification
NOTIFY_TYPES = {"complete": "success", "object_complete": "success",
                "calibration": "success", "chamber_reached": "success",
                "chamber_fault": "warning",
                "driver_hot": "warning", "error": "failure",
                "warning": "failure", "shutdown": "failure",
                "power_loss": "failure", "filament_short": "failure",
                "cancelled": "warning", "paused": "warning",
                "file_pause": "warning",
                "filament_low": "warning", "spool_switch": "warning",
                "slot_empty": "warning"}
FILE_CAP = 30
# Emails about one print go in one thread (see _email_prepare). Faults
# join it while printing and this long after the print ended.
THREAD_ROWS = ("started", "preprint_done", "layer_started", "first_layer",
               "progress_25", "progress_50",
               "progress_75", "status_report", "paused", "file_pause", "resumed", "object_complete",
               "complete", "cancelled",
               "chamber_waiting", "chamber_reached", "chamber_fault",
               "filament_change", "filament_short", "filament_low",
               "spool_switch", "slot_empty")
THREAD_FAULT_WINDOW = 120.
NAME_CAP_MIN = 8
PTC_FIELDS = ["extruder_map_table", "filament_spool_id", "extruders_used",
              "auto_replenish_filament", "replenish_ignore_color",
              "filament_exist", "filament_vendor", "filament_type",
              "filament_sub_type", "filament_color_rgba",
              "filament_color_multi"]
# The pre-print calibrations chosen for a job (printers that have them)
PREPRINT_FIELDS = ["auto_bed_leveling", "flow_calibrate",
                   "flow_calib_extruders", "shaper_calibrate",
                   "extruders_used"]
# The printer refills only from a toolhead with the same nozzle
# (extruders: printers.py)
NOZZLE_FIELDS = ["nozzle_diameter", "nozzle_volume_type"]
# A job that starts this far into its file is a power-loss recovery
RECOVERY_MIN_PROGRESS = 0.01
# ...and either kept its print time, or is this far in (a small file's
# start G-code alone is past 1 %)
RECOVERY_MIN_TIME = 30.
RECOVERY_SURE_PROGRESS = 0.10
# Below this file progress, time left comes from the slicer estimate rather
# than print time / progress (the first layer alone makes that ratio useless)
EARLY_PROGRESS = 0.10
SEND_RETRY_DELAYS = (6, 10, 30)
# ntfy timestamps messages to the second and its app orders by that time:
# two sends this far apart can't share a second and swap places
SEND_GAP = {"ntfy": 2.}   # seconds before each retry of a failed send
# Within one service (services never wait on each other), faults go ahead of
# everything else, and these status notifications are dropped if they could
# not be sent this long after they happened (e.g. a long outage): a 25%
# notification an hour late is noise. Faults and complete never expire.
URGENT_ROWS = ("error", "warning", "shutdown", "power_loss",
               "chamber_fault")
EXPIRING_ROWS = ("started", "preprint_done", "layer_started", "first_layer",
                 "progress_25",
                 "progress_50", "progress_75", "object_complete",
                 "chamber_waiting", "chamber_reached", "filament_change")
STATUS_EXPIRY = 300.
# Spoolman usage counts as tracked once spools lost this much; if nothing
# was used by this far into the file, it isn't tracked and the filament
# checks during the print job are skipped (the one at the start still runs)
USAGE_MIN_G = 0.5
USAGE_GRACE = 0.05
# Wait this long for the server's answer (Apprise default: 4 s), so a slow
# upload that the server accepted is not counted as failed and sent twice.
SEND_READ_TIMEOUT = 60.
# Apprise's default (4 s) is short for a slow or relayed connection
SEND_CONNECT_TIMEOUT = 10.
# (power_loss_env_paths: printers.py)
PL_WAIT_TIME = 60.
BOOT_WINDOW = 600.
# After boot the camera service may still be starting: the power-loss
# snapshot is retried this long before sending without it.
PL_CAMERA_WAIT = 45.
PL_CAMERA_POLL = 5.

FaultKey = Tuple[Any, Any, Any]
GCODE_EXTS = (".gcode", ".gco", ".g")
# The X and Y stepper drivers report their own temperature (TMC2240 ADC);
# the alert goes once per print job, and again after a pause (so a print
# job paused for the heat and resumed can warn a second time)
# (drivers: printers.py, found from the object list)
DRIVER_HOT_DEFAULT = 105
# Print-by-object (Orca "print_sequence = by object"): the slicer's config
# block at the end of the file says so, and its EXCLUDE_OBJECT markers tell
# Klipper which object is being printed. Layer numbers run on across the
# objects, so an object's own layer is counted from the layer it began on.
CONFIG_TAIL = 256 * 1024
# Pauses the print job itself asks for, put there by the slicer ("add
# pause", "change filament") or by hand: PAUSE, M601, M226, M0, M1, M600,
# and Orca's ;PAUSE_PRINT marker (followed by the printer's pause G-code).
# A marker and a command within PAUSE_MERGE bytes are one pause. The
# pause is told from a person's by virtual_sdcard.file_position, which
# sits just past the pause line when the file did it.
RX_PAUSE_CMD = re.compile(rb"^(PAUSE|M601|M226|M0|M1|M600)\b[ \t]*([^;\r\n]*)"
                          rb"(?:;[ \t]*([^\r\n]*))?", re.I)
RX_PAUSE_MARK = re.compile(rb"^;[ \t]*PAUSE_PRINT\b", re.I)
RX_LAYER = re.compile(rb"^;[ \t]*(LAYER_CHANGE|LAYER:[ \t]*(\d+))", re.I)
RX_M117 = re.compile(rb"^M117[ \t]+([^\r\n]*)", re.I)
PAUSE_MERGE = 400
PAUSE_WINDOW = 2048
RX_BY_OBJECT = re.compile(rb"^; *print_sequence *= *by object *$", re.M)
RX_OBJECT_NAME = re.compile(r"^(.*?)_id_(\d+)_copy_(\d+)$")


def shown(filename: str) -> str:
    """File name as shown to people: without the G-code extension (Klipper
    only prints .gcode, .gco and .g files)."""
    low = filename.lower()
    for ext in GCODE_EXTS:
        if low.endswith(ext) and len(filename) > len(ext):
            return filename[:-len(ext)]
    return filename


def format_code(ex: Dict[str, Any]) -> str:
    try:
        return "%04d-%04d-%04d-%04d" % (
            int(ex["level"]), int(ex["id"]), int(ex["index"]),
            int(ex["code"]))
    except (KeyError, TypeError, ValueError):
        return "-".join(str(ex.get(k)) for k in ("level", "id", "index",
                                                  "code"))


# Klipper's fault messages are internal ("detected noodle"). The printer's
# screen shows its own text per code, from the firmware's exception text
# file ("<id>-<index>-<code>": "<title>_<details>"); notifications use the
# same. Faults it doesn't list keep Klipper's message.
# (exception_text_paths: printers.py; set by the component once the
# printer is known)
EXCEPTION_TEXT_PATHS: Tuple[str, ...] = tuple(
    p for cls in printers.PRINTERS for p in cls.exception_text_paths)
_exception_text: Optional[Dict[str, str]] = None


def exception_text() -> Dict[str, str]:
    global _exception_text
    if _exception_text is None:
        _exception_text = {}
        for path in EXCEPTION_TEXT_PATHS:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _exception_text = {str(k): str(v) for k, v in data.items()}
                    break
            except Exception as e:
                logging.info("[pushraker] no exception texts in "
                             "%s: %s", path, e)
    return _exception_text


# An emoji before the error code for some kinds of fault, by module id and
# code (any toolhead), going by each code's text in exception_en-GB.json.
# Codes not listed get none.
def _codes(module: int, *codes: Any) -> Set[Tuple[int, int]]:
    out: Set[Tuple[int, int]] = set()
    for c in codes:
        for n in (c if isinstance(c, range) else (c,)):
            out.add((module, n))
    return out


FAULT_EMOJI: Dict[Tuple[int, int], str] = {}
for _emoji, _set in (
    # Temperature: heating module, nozzle, heated bed, chamber
    ("🌡️", _codes(522, 15) | _codes(523, 1, 2, 3, 36) | _codes(526, 0, 1)
     | _codes(527, 0) | _codes(533, 1)),
    ("💾", _codes(531, 9)),                                # storage space
    ("☁️", _codes(531, 7, 11, 12)),                        # cloud print file
    # Calibration (vibration compensation, flow, XYZ offset, homing)
    ("📐", _codes(522, 21) | _codes(523, 9000, 9001)
     | _codes(530, range(0, 10), 18, 19, 20, 23, range(24, 29))),
    ("🍝", _codes(532, 2)),                                # spaghetti
    # Spool and filament: runout, filament info, feeder loading/unloading
    ("🧵", _codes(523, 0, 39) | _codes(525, 0, 1, 2, 3, 5, 6, 8,
                                         range(10, 16), range(30, 36), 70,
                                         90)),
    # Toolhead: swapping, picking, parking, detached, placement, Pogopin,
    # maintenance
    ("🔧", _codes(523, range(7, 36), range(37, 38), range(40, 48))
     | _codes(528, 13, 14) | _codes(530, range(13, 17))),
):
    for _k in _set:
        FAULT_EMOJI[_k] = _emoji


def fault_emoji(ex: Dict[str, Any]) -> str:
    try:
        return FAULT_EMOJI.get((int(ex.get("id")), int(ex.get("code"))), "")
    except (TypeError, ValueError):
        return ""


def describe(ex: Dict[str, Any]) -> str:
    """Error code and the text the printer shows for it, e.g.
    "0002-0532-0000-0002: Possible Spaghetti Defect Detected" and the
    screen's advice on the next line."""
    msg = str(ex.get("message") or "").strip()
    try:
        key = "%04d-%04d-%04d" % (int(ex.get("id")), int(ex.get("index")),
                                  int(ex.get("code")))
    except (TypeError, ValueError):
        key = ""
    text = exception_text().get(key, "")
    title, _, details = text.partition("_")
    emoji = fault_emoji(ex)
    code = f"{emoji} {format_code(ex)}" if emoji else format_code(ex)
    if not title.strip():
        return f"{code}: {msg}"
    return "\n".join(x for x in (f"{code}: {title.strip()}",
                                 details.strip()) if x)


def coded(ps: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The Snapmaker fault recorded in print_stats, if any."""
    ex = ps.get("exception") or {}
    if isinstance(ex, dict) and ex.get("code") is not None:
        return ex
    return None


def fault_key(ex: Dict[str, Any]) -> FaultKey:
    return (ex.get("id"), ex.get("index"), ex.get("code"))


def format_duration(seconds: float) -> str:
    t = int(seconds or 0)
    if t >= 3600:
        return "%dh %02dm" % (t // 3600, (t % 3600) // 60)
    return "%dm" % (t // 60)


class ChannelGate:
    """One service's send order. Each notification takes a ticket when it
    happens: (urgency, sequence). A send attempt waits until its ticket is
    the first one still pending and nothing else is sending, so
    notifications arrive in the order they happened (resumed never before
    paused), except that faults go ahead of status notifications. A ticket
    is held until its notification is sent or given up, so one that is
    waiting to retry holds the channel's later notifications back (they
    would arrive out of order otherwise); faults, with a lower ticket, go
    ahead of it. Services never wait on each other."""

    def __init__(self) -> None:
        self._seq = 0
        self._tickets: Set[Tuple[int, int]] = set()
        self._busy: Optional[Tuple[int, int]] = None
        self._cond: Optional[asyncio.Condition] = None
        self.last_sent = -1e9   # monotonic time the last attempt ended

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def ticket(self, urgent: bool) -> Tuple[int, int]:
        self._seq += 1
        t = (0 if urgent else 1, self._seq)
        self._tickets.add(t)
        return t

    async def acquire(self, t: Tuple[int, int]) -> None:
        cond = self._condition()
        async with cond:
            await cond.wait_for(
                lambda: self._busy is None and min(self._tickets) == t)
            self._busy = t

    async def release(self, t: Tuple[int, int]) -> None:
        cond = self._condition()
        async with cond:
            if self._busy == t:
                self._busy = None
            cond.notify_all()

    async def done(self, t: Tuple[int, int]) -> None:
        cond = self._condition()
        async with cond:
            self._tickets.discard(t)
            if self._busy == t:
                self._busy = None
            cond.notify_all()


class Ticket(NamedTuple):
    gate: ChannelGate
    ticket: Tuple[int, int]
    created: float              # monotonic time the notification happened


# Console lines (Fluidd/Mainsail): what was sent, to which services, and
# each failed attempt with a few words on why. Failed attempts of one
# notification with the same cause, at the same attempt, within this many
# seconds are one line.
CONSOLE_GROUP = 1.
CHANNEL_NAMES = {"ntfy": "ntfy", "gotify": "Gotify", "pushover": "Pushover",
                 "telegram": "Telegram", "discord": "Discord",
                 "email": "email", "webhook": "webhook"}
CHANNEL_ORDER = list(CHANNEL_NAMES)
# Which service an Apprise log line is about: the file that logged it
APPRISE_SOURCES = (("discord.py", "discord"),
                   ("email", "email"), ("ntfy", "ntfy"),
                   ("gotify", "gotify"), ("pushover", "pushover"),
                   ("telegram", "telegram"), ("discord", "discord"),
                   ("json", "webhook"))
SMTP_PHRASES = {421: "server busy", 450: "mailbox busy", 451: "server busy",
                530: "login required", 534: "login rejected",
                535: "login rejected", 550: "rejected", 553: "rejected",
                554: "rejected"}


def short_cause(messages: List[str]) -> str:
    """Why a send failed, in a few words, from what Apprise logged:
    "401 Unauthorized", "timed out", "SSL error", "535 login rejected"."""
    text = " | ".join(messages)
    low = text.lower()
    m = re.search(r"error=(\d{3})\b", text)
    if m:
        code = int(m.group(1))
        try:
            return f"{code} {http.HTTPStatus(code).phrase}"
        except ValueError:
            return f"HTTP {code}"
    m = re.search(r"\((\d{3}), b['\"]", text)
    if m:
        code = int(m.group(1))
        return f"{code} {SMTP_PHRASES.get(code, 'rejected by server')}"
    if "timed out" in low or "timeout" in low:
        return "timed out"
    if "ssl" in low or "certificate" in low:
        return "SSL error"
    if any(k in low for k in ("name or service not known", "name resolution",
                              "nodename nor servname", "getaddrinfo",
                              "no address associated", "failed to resolve")):
        return "server not found"
    if "connection refused" in low:
        return "connection refused"
    if "connect" in low:
        return "could not connect"
    return "failed"


class AppriseLog(logging.Handler):
    """Apprise's messages per service (debug ones too), to say why a send
    failed. Moonraker's log gets what it got before: Apprise's own logger
    no longer passes records up, and this passes on those at the level it
    logged at (debug lines can hold URLs with tokens)."""
    installed: Optional["AppriseLog"] = None

    def __init__(self, level: int) -> None:
        super().__init__(logging.DEBUG)
        self.forward_level = level
        self.messages: Dict[str, List[str]] = {}

    @classmethod
    def install(cls) -> "AppriseLog":
        if cls.installed is None:
            log = logging.getLogger("apprise")
            cls.installed = cls(log.getEffectiveLevel())
            log.addHandler(cls.installed)
            log.setLevel(logging.DEBUG)
            log.propagate = False
        return cls.installed

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= self.forward_level:
            logging.getLogger().handle(record)
        path = (record.pathname or "").replace("\\", "/")
        where = path.rsplit("/apprise/", 1)[-1] if "/apprise/" in path \
            else os.path.basename(path)
        for key, channel in APPRISE_SOURCES:
            if key in where:
                try:
                    msgs = self.messages.setdefault(channel, [])
                    msgs.append(record.getMessage()[:500])
                    del msgs[:-20]
                except Exception:
                    pass
                return

    def take(self, channel: str) -> List[str]:
        return self.messages.pop(channel, [])


class Destination(NamedTuple):
    channel: str             # "ntfy", "pushover", "email", ... (for logs)
    ap: apprise.Apprise
    email: bool


def has_network() -> bool:
    """True if any interface other than loopback has an active route.

    Read from /proc, so it costs nothing and sends nothing. Unknown (no
    /proc) counts as a network."""
    try:
        with open("/proc/net/route") as f:
            next(f, None)
            for line in f:
                fields = line.split()
                if len(fields) > 3 and fields[0] != "lo" and \
                        int(fields[3], 16) & 0x1:
                    return True
        return False
    except (OSError, ValueError):
        return True


class PushNotifications:
    def __init__(self, config: ConfigHelper) -> None:
        self.apprise_log = AppriseLog.install()
        # Failed attempts waiting CONSOLE_GROUP to be joined into one line
        self._console_pending: Dict[Tuple[Any, ...], List[str]] = {}
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.config_name = config.get_name()
        try:
            app_args = self.server.get_app_args()
        except Exception:
            app_args = {}
        data_path = app_args.get("data_path") or os.path.expanduser(
            "~/printer_data")
        config_dir = os.path.dirname(app_args.get("config_file") or "") \
            or os.path.join(data_path, "config")
        self._low_path = os.path.join(data_path, ".pushraker.json")
        # 0.1 kept it under this name; moved on first read
        self._old_low_path = os.path.join(data_path, ".push_notifications.json")
        # Where the settings come from: pushraker.conf (the page edits it,
        # the component re-reads it), or, when the [pushraker]
        # section itself carries notification options, those (the old
        # generated config, and the tests)
        self.store: Optional[cfgmod.Store] = None
        self.settings: Dict[str, Any] = {}
        self.env = cfgmod.Env()
        self.error_index_url: str = config.get("error_index_url", "")
        # A harness moving users over from an older setup: settings to read
        # when there is no pushraker.conf yet ("<file> <section>", INI; the
        # secrets likewise), and files that setup generated, renamed to
        # <file>.old at start so they don't load next to pushraker
        self.import_settings = config.get("import_settings", "").split()
        self.import_secrets = config.get("import_secrets", "").split()
        self.retire_files = [p.strip() for p in
                             config.get("retire_files", "").split(",")
                             if p.strip()]
        self.hook_values: Dict[str, int] = {}
        self._hooks_warned = False
        legacy = config.get("settings", "file") == "options" or any(
            config.get(k, None) is not None for rid in ROW_IDS
            for k in (rid, f"{rid}_email", f"{rid}_webhook"))
        if not legacy:
            self.store = cfgmod.Store(
                config.get("config_dir", config_dir),
                config.get("data_dir", data_path),
                name=config.get("name", "Printer"))
        self._apply_options(self._legacy_options(config) if legacy else {})
        self._init_state()
        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_event)
        for event in EXCEPTION_EVENTS:
            self.server.register_event_handler(event, self._on_exception)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._on_klippy_shutdown)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)
        self.server.register_remote_method(REMOTE_METHOD, self._on_remote)
        self.server.register_remote_method(OLD_REMOTE_METHOD, self._on_remote)
        self.server.register_event_handler(
            "server:gcode_response", self._on_gcode_response)
        if self.store is not None:
            # /server/pushraker/...; /server/push_notifications/... is what
            # 0.1.x answered on, kept so nothing that used it breaks
            for base in ("/server/pushraker", "/server/push_notifications"):
                for path, methods, handler in (
                        ("config", ["GET", "POST"], self._ep_config),
                        ("test", ["POST"], self._ep_test),
                        ("objects", ["GET"], self._ep_objects),
                        ("preview", ["POST"], self._ep_preview),
                        ("status", ["GET"], self._ep_status)):
                    try:
                        self.server.register_endpoint(
                            f"{base}/{path}", methods, handler)
                    except Exception as e:
                        logging.error("[pushraker] endpoint "
                                      "%s/%s: %s", base, path, e)
            self._spawn(self._watch_settings())
            self._retire()

    async def close(self) -> None:
        """Moonraker shutting down or restarting (it reuses the process):
        stop the settings watcher and whatever else is in flight."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def _legacy_options(self, config: ConfigHelper) -> Dict[str, str]:
        """The options of a [pushraker] section that carries the
        notifications itself (URLs may refer to Moonraker's secrets)."""
        out: Dict[str, str] = {}
        for key in ("name", "snapshot_url", "layers_complete",
                    "first_layer_objects", "filament_change_events",
                    "timezone", "min_level", "settle_time", "repeat_interval",
                    "spoolman_url", "filament_low_percent",
                    "filament_low_grams", "filament_unknown", "driver_temp",
                    "web_ui_url", "camera_url", "macro_priority",
                    "error_index_url"):
            v = config.get(key, None)
            if v is not None:
                out[key] = str(v)
        for rid in ROW_IDS:
            for key in (rid, f"{rid}_email", f"{rid}_webhook"):
                v = self._option(config, key)
                if v:
                    out[key] = v
        for rid in set(ROW_IDS) | set(DATA_ALIAS.values()) | {"default"}:
            v = config.get(f"data_{rid}", None)
            if v is not None:
                out[f"data_{rid}"] = str(v)
        return out

    def _apply_options(self, opts: Dict[str, str]) -> None:
        """Set everything that depends on the settings (the options the
        generated Moonraker config used to hold). Safe to call again: a
        changed settings file replaces the destinations in place."""
        def get(key: str, default: str) -> str:
            return str(opts.get(key, default))

        def num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return min(hi, max(lo, float(get(key, str(default)))))
            except ValueError:
                return default
        # The destinations first: a bad address leaves everything as it was
        rows: Dict[str, Tuple[str, List[Destination]]] = {}
        for rid in ROW_IDS:
            image = ""
            dests: List[Destination] = []
            for key, email in ((rid, False), (f"{rid}_email", True),
                               (f"{rid}_webhook", False)):
                value = opts.get(key)
                if not value:
                    continue
                kind, _, url = value.partition(",")
                kind, url = kind.strip(), url.strip()
                if kind not in IMAGE_KINDS or not url:
                    raise ValueError(
                        f"{key}: expected '<render|snapshot|spool|none>, "
                        "<url>'")
                ap = apprise.Apprise()
                if not ap.add(url):
                    raise ValueError(f"{key}: invalid notification URL")
                for srv in ap:
                    srv.socket_read_timeout = max(
                        srv.socket_read_timeout, SEND_READ_TIMEOUT)
                    srv.socket_connect_timeout = max(
                        srv.socket_connect_timeout, SEND_CONNECT_TIMEOUT)
                channel = "email" if email else url.split(":", 1)[0]
                channel = {"ntfys": "ntfy", "pover": "pushover",
                           "gotifys": "gotify",
                           "tgram": "telegram", "discordhook": "discord",
                           "discorddm": "discord", "json": "webhook",
                           "jsons": "webhook"}.get(
                    channel, channel)
                image = image or kind
                dests.append(Destination(channel, ap, email))
            if dests:
                rows[rid] = (image, dests)
        self.rows = rows
        self.name: str = get("name", "Printer")
        self.snapshot_url: Optional[str] = opts.get("snapshot_url") or None
        # "Layer N complete" is reported when layer N + 1 starts
        self.layers_complete: int = int(num("layers_complete", 2, 1, 3))
        # Print-by-object: "layer N complete" for each object, or once
        self.first_layer_objects: str = get("first_layer_objects", "each")
        # Which of loaded / unloaded the Filament loaded / unloaded row sends
        self.filament_change_events = {
            e.strip() for e in get(
                "filament_change_events", "loaded,unloaded").split(",")}
        self.tz: Optional[datetime.tzinfo] = None
        tz_name = get("timezone", "")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(tz_name)
            except Exception as e:
                logging.warning("[pushraker] time zone %s "
                                "unavailable, ETA shown as time left only: %s",
                                tz_name, e)
        self.min_level: int = int(num("min_level", 1, 1, 9))
        self.settle_time: float = num("settle_time", 3., 0., 3600.)
        self.repeat_interval: float = num("repeat_interval", 60., 0., 1e9)
        self.spoolman_url: str = get("spoolman_url", "").rstrip("/")
        self.filament_low_percent: float = num(
            "filament_low_percent", 10., 0., 100.)
        self.filament_low_grams: float = num("filament_low_grams", 50., 0., 1e9)
        # Warn at print start about a loaded spool Spoolman shows at 0 g
        # ("amount unknown"); off for people who don't track weights
        self.filament_unknown: bool = get(
            "filament_unknown", "true").strip().lower() in ("true", "1", "yes")
        # The option is driver_temp: driver_hot is the row
        self.driver_hot: float = num(
            "driver_temp", DRIVER_HOT_DEFAULT, 40., 150.)
        # Status report: every this many minutes of print time
        self.report_interval: float = num("report_interval", 30., 1., 720.)
        self.web_ui_url: str = get("web_ui_url", "")
        self.camera_url: str = get("camera_url", "")
        if opts.get("error_index_url"):
            self.error_index_url = opts["error_index_url"]
        # RESPOND PREFIX=notify MSG="title || message || priority"
        self.macro_priority: str = get("macro_priority", "default")
        # Data added to the end of each row's notifications (templating.py),
        # by the settings' row id
        self.data: Dict[str, str] = {
            k[5:]: v for k, v in opts.items()
            if k.startswith("data_") and v}
        # Time-left method: "" means the printer's default
        self.time_left_method: str = get("time_left", "")
        logging.info("[pushraker] enabled: %s", ", ".join(
            f"{rid} ({'+'.join(d.channel for d in dests)})"
            for rid, (_, dests) in self.rows.items()) or "none")

    def _init_state(self) -> None:
        self._driver_hot_sent = False
        self._filament_job = 0      # bumped per job; stops an old watcher
        self._job_map: List[Any] = []       # extruder_map_table at start
        self._job_tools: Set[int] = set()   # slicer filaments the job uses
        self._job_weights: Dict[int, float] = {}  # grams per slicer filament
        self._switched_to: Set[int] = set() # channels refilled from
        self._job_ptc: Dict[str, Any] = {}  # print_task_config at start
        # Filament still needed is measured, not estimated: the slicer's
        # total for each slicer filament minus what Spoolman says its spools
        # used since the print job started (see _need_now)
        self._job_progress0 = 0.            # file progress the watch began at
        self._spool_start: Dict[int, float] = {}   # spool id -> g at start
        self._spool_now: Dict[int, float] = {}     # spool id -> g, latest
        self._tool_spools: Dict[int, Set[int]] = {}  # slicer filament -> spools
        # Email thread of the current print: {"id", "file"}; kept across
        # restarts so a power-loss recovery continues the same thread
        self._thread: Optional[Dict[str, str]] = None
        self._job_ended = -1e9   # monotonic time the last job finished
        # Bumped when a job starts or ends: an await that outlives its job
        # (seeding, the filament watch) checks it and gives up
        self._job_gen = 0
        # Klipper keeps print_stats.exception after a fault-pause is
        # resumed; the fault is over, and must not colour a later pause or
        # cancel. A new fault arrives as an exception event and clears this.
        self._stale_fault: Optional[FaultKey] = None
        self._pl_file = ""       # file of the print a power loss stopped
        # Spools already reported as running low (spool id -> time)
        self._low_sent: Optional[Dict[str, float]] = None
        self._tasks: Set[asyncio.Task] = set()
        # Faults already reported: key -> monotonic time
        self._faults_sent: Dict[FaultKey, float] = {}
        # Send order per channel (see ChannelGate); channels are independent
        self._gates: Dict[str, ChannelGate] = {}
        self._faults_pending: Set[FaultKey] = set()
        self._last_fault_send = 0.
        self._ready_once = False
        # Per-job state
        self._job_active = False
        self._job_done_sent = False
        self._first_layer_sent = False
        self._layer_started_sent = False
        # Pre-print calibrations of this job: their names (for the started
        # notification), the ones still to run, the one running now
        self._pp_names: List[str] = []
        self._pp_pending: set = set()
        self._pp_running: Optional[Tuple[str, Optional[int]]] = None
        self._pp_sent = True
        self._milestones_sent: Set[int] = set()
        # Print time (which stops while paused) the next status report is
        # due at; None until the job's first status update sets it
        self._report_next: Optional[float] = None
        self._gcode_bytes: Tuple[Any, Any] = (None, None)
        self._job_filename = ""
        # Print-by-object: the object being printed, the layer each began
        # on, and the objects already reported
        self._by_object = False
        # Pauses in the print job's file (see _scan_pauses) and the ones
        # already reported, by index
        self._job_pauses: List[Dict[str, Any]] = []
        self._pauses_seen: Set[int] = set()
        self._obj_cur: Optional[str] = None
        self._obj_start: Dict[str, int] = {}
        self._obj_first_sent: Set[str] = set()
        # The printer behind the object list, known at Klipper's ready
        self.printer: printers.Printer = printers.Printer([])
        # The slicer's settings from the job's file (per-filament
        # temperatures, materials, names; templating.slicer_settings)
        self._job_slicer: Dict[str, str] = {}
        # The file's M73 R lines (byte offset -> minutes remaining), for the
        # m73 method; the print time when layer 1 began (first_layer)
        self._job_m73: List[Tuple[int, float]] = []
        self._layer1_pd: Optional[float] = None
        self._status: Dict[str, Dict[str, Any]] = {
            "print_stats": {}, "virtual_sdcard": {}, "print_task_config": {},
            "exclude_object": {}, "machine_state_manager": {},
            "idle_timeout": {}, ACE_OBJECT: {}}
        self._cal_busy_at = 0.      # when Klipper was last seen busy
        self._idle_was = ""         # idle_timeout.state last seen
        # Filament loaded / unloaded: last reported filament_exist per
        # toolhead (None until seeded at klippy_ready), the filament each
        # toolhead last had (to name what was unloaded), pending changes
        self._exist_seen: Optional[List[bool]] = None
        self._exist_names: Dict[int, str] = {}
        self._exist_gen = 0
        # Chamber: the printer's heater_generic objects, whether it has
        # Snapmaker's chamber sensor / purifier / a DragonBreath (found at
        # klippy_ready), and this job's chamber state (see _track_chamber)
        self._ch_heaters: List[str] = []
        self._ch_sensor = False
        self._ch_purifier = False
        self._ch_device = False
        self._ch: Dict[str, Any] = {}
        self._calibrating: Optional[int] = None  # main_state of a running one
        self._refills = 0      # refills from a backup spool seen so far
        # multiACE (see _track_ace): found at klippy_ready; the last
        # head_source seen; heads with a runout being held; slots already
        # reported empty this job
        self._ace_on = False
        self._ace_src: Dict[str, Any] = {}
        self._ace_runouts: Set[int] = set()
        self._slot_empty_sent: Set[Tuple[int, int]] = set()
        self._cal_action: Optional[int] = None   # its last action_code
        self._cal_hold = 0                       # bumps on every action change

    # ---- settings: pushraker.conf, the page's endpoints ---------------------

    def _first_settings(self) -> Dict[str, Any]:
        assert self.store is not None
        if len(self.import_settings) == 2:
            sec = self.import_secrets if len(self.import_secrets) == 2 \
                else ["", ""]
            try:
                s = cfgmod.import_ini(self.import_settings[0],
                                      self.import_settings[1], sec[0], sec[1],
                                      name=self.store.default_name)
                if s["enabled"] or s["server"] or s["secret"]:
                    logging.info("[pushraker] settings moved over "
                                 "from %s [%s]", *self.import_settings)
                    return s
            except Exception as e:
                logging.warning("[pushraker] older settings not "
                                "read: %s", e)
        return self.store.load()

    def _retire(self) -> None:
        for path in self.retire_files:
            if os.path.isfile(path):
                try:
                    os.replace(path, path + ".old")
                    logging.info("[pushraker] %s renamed to %s.old "
                                 "(an older setup's; pushraker replaces it)",
                                 path, path)
                except OSError as e:
                    logging.warning("[pushraker] %s: %s", path, e)

    async def _env(self) -> cfgmod.Env:
        """What the printer offers right now."""
        cams: Dict[str, Dict[str, str]] = {}
        try:
            wc = self.server.lookup_component("webcam", None)
            if wc is not None:
                found = wc.get_webcams()
                if inspect.isawaitable(found):    # sync in Moonraker 0.9+
                    found = await found
                for name, cam in dict(found).items():
                    if not getattr(cam, "enabled", True):
                        continue
                    snap = str(getattr(cam, "snapshot_url", "") or "")
                    stream = str(getattr(cam, "stream_url", "") or "")
                    if snap:
                        # Moonraker roots a partial URL on its own default
                        # host and turns a local address into 127.0.0.1
                        try:
                            snap = await cam.get_snapshot_url(
                                convert_local=True)
                        except Exception:
                            if "://" not in snap:
                                snap = "http://127.0.0.1/" + snap.lstrip("/")
                    cams[str(name)] = {"snapshot": snap, "stream": stream}
        except Exception as e:
            logging.warning("[pushraker] webcams: %s", e)
        spoolman = ""
        try:
            sm = self.server.lookup_component("spoolman", None)
            url = str(getattr(sm, "spoolman_url", "") or "") if sm else ""
            spoolman = re.sub(r"/api(/v\d+)?/?$", "", url.rstrip("/"))
        except Exception:
            spoolman = ""
        return cfgmod.Env(cameras=cams, spoolman_url=spoolman,
                          error_index_url=self.error_index_url,
                          name=self.store.default_name if self.store
                          else self.name,
                          rows_unavailable=self.printer.rows_unavailable,
                          preprint=bool(self.printer.preprint_calibrations),
                          time_methods=[
                              {"id": m, "label": l, "help": h}
                              for m, (l, h) in self._time_methods().items()],
                          time_default=self.printer.time_default)

    async def _reload(self, why: str) -> None:
        """Read pushraker.conf and apply it. Settings that don't pass
        validation keep the last applied ones (the page shows why)."""
        if self.store is None:
            return
        self.env = await self._env()
        self.settings = self.store.load()
        try:
            err = cfgmod.stored_error(self.settings, self.env)
        except Exception as e:
            err = f"unreadable ({e})"
        if err:
            logging.warning("[pushraker] settings need attention "
                            "(%s); keeping the last applied ones", err)
            return
        try:
            opts = cfgmod.render_options(self.settings, self.env)
            self._apply_options(opts)
        except Exception as e:
            logging.error("[pushraker] settings not applied: %s", e)
            return
        logging.info("[pushraker] settings applied (%s)", why)
        await self._push_hooks()

    async def _watch_settings(self) -> None:
        """Re-read the settings file when it is edited by hand."""
        while True:
            try:
                await asyncio.sleep(SETTINGS_POLL)
                if self.store is not None and self.store.changed() \
                        and self._ready_once:
                    await self._reload("file changed")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logging.error("[pushraker] settings watch: %s", e)

    async def _push_hooks(self) -> None:
        """Tell the Klipper print hooks what to do (SET_GCODE_VARIABLE on
        _NTFY_HOOKS, see klipper/pushraker.cfg): 0 nothing, 1 report, 2
        lift for the snapshot and report. Nothing to do when the macro
        file isn't included."""
        if self.store is None or not self._ready_once:
            return
        values = cfgmod.hook_values(self.settings, self.env)
        values["lift_ceiling"] = self.printer.lift_ceiling
        if values == self.hook_values:
            return
        kapis: APIComp = self.server.lookup_component("klippy_apis")
        try:
            objs = await kapis.get_object_list(default=[])
            if f"gcode_macro {HOOKS_MACRO}" not in objs:
                if not self._hooks_warned:
                    self._hooks_warned = True
                    logging.warning(
                        "[pushraker] Klipper has no %s macro: "
                        "include pushraker.cfg in printer.cfg for the "
                        "print-end and cancel snapshots", HOOKS_MACRO)
                return
            for key, value in values.items():
                await kapis.run_gcode(
                    f"SET_GCODE_VARIABLE MACRO={HOOKS_MACRO} "
                    f"VARIABLE={key} VALUE={value}")
            self.hook_values = values
        except Exception as e:
            logging.warning("[pushraker] print hooks not set: %s", e)

    def _busy(self) -> bool:
        return self._status["print_stats"].get("state") in (
            "printing", "paused")

    async def _view(self) -> Dict[str, Any]:
        assert self.store is not None
        return cfgmod.public_view(self.settings, self.env, self._busy(),
                                  __version__)

    async def _refresh(self) -> None:
        """Before answering the page: pick up a hand edit (and apply it,
        like the watcher would) and the printer's current webcams."""
        assert self.store is not None
        if self.store.changed() or not self.settings:
            if self._ready_once:
                await self._reload("file changed")
            else:
                self.env = await self._env()
                self.settings = self.store.load()
        else:
            self.env = await self._env()

    async def _ep_config(self, web_request: Any) -> Dict[str, Any]:
        assert self.store is not None
        await self._refresh()
        if web_request.get_action() == "GET":
            return await self._view()
        data = web_request.get_args()
        try:
            notes = await self._save(dict(data))
        except cfgmod.ConfigError as e:
            raise self.server.error(str(e), 400)
        view = await self._view()
        view["notes"] = notes
        return view

    async def _save(self, data: Dict[str, Any]) -> List[str]:
        """Validate, look up what the page can't (Telegram chat, Discord
        owner), check the addresses with Apprise, store, apply live."""
        assert self.store is not None
        s = cfgmod.validate(data, self.settings, self.env)
        notes: List[str] = []
        if s["enabled"]:
            for lookup in (cfgmod.telegram_find_chat,
                           cfgmod.discord_find_user):
                line = await self.eventloop.run_in_thread(lookup, s)
                if line:
                    notes.append(line)
        try:
            opts = cfgmod.render_options(s, self.env)
        except ValueError as e:
            raise cfgmod.ConfigError(f"Invalid settings: {e}")
        for key, value in opts.items():
            if "," not in value or not value.split(",", 1)[1].strip():
                continue
            if key in ROW_IDS or key.endswith(("_email", "_webhook")) \
                    or key.startswith(("macro_", "object_", "file_pause")):
                url = value.split(",", 1)[1].strip()
                if not apprise.Apprise().add(url):
                    what = ("email" if key.endswith("_email") else
                            "webhook" if key.endswith("_webhook") else
                            "push service")
                    raise cfgmod.ConfigError(
                        f"The {what} settings don't make a valid address; "
                        "check them (server, user name, addresses)")
        try:
            self.store.save(s)
        except OSError as e:
            raise cfgmod.ConfigError(f"Could not write the settings: {e}. "
                                     "Nothing was saved.")
        self.settings = s
        self._apply_options(opts)
        await self._push_hooks()
        if cfgmod.push_configured(s) and s["provider"] == "gotify":
            notes.append("Text only: picture attachments not supported by "
                         "Gotify.")
        if cfgmod.configured(s) and s["camera"] and \
                not cfgmod.camera(s, self.env):
            notes.append(f'No webcam named "{s["camera"]}" in Moonraker; '
                         "notifications are sent without camera images.")
        return notes

    async def _ep_status(self, web_request: Any) -> Dict[str, Any]:
        assert self.store is not None
        await self._refresh()
        return {"version": __version__,
                "enabled": bool(self.settings.get("enabled")),
                "summary": cfgmod.summary(self.settings, self.env),
                "busy": self._busy(),
                "rows": sorted(self.rows),
                "cameras": sorted(self.env.cameras),
                "spoolman": bool(self.env.spoolman_url)}

    async def _ep_test(self, web_request: Any) -> Dict[str, Any]:
        """Send a test notification with the settings the page holds (not
        necessarily saved) to every destination they set up."""
        assert self.store is not None
        data = dict(web_request.get_args())
        data["enabled"] = True
        await self._refresh()
        try:
            s = cfgmod.validate(data, self.settings, self.env)
            for lookup in (cfgmod.telegram_find_chat,
                           cfgmod.discord_find_user):
                await self.eventloop.run_in_thread(lookup, s)
            return await self._run_test(s)
        except cfgmod.ConfigError as e:
            raise self.server.error(str(e), 400)

    async def _run_test(self, s: Dict[str, Any]) -> Dict[str, Any]:
        env = self.env
        lines: List[str] = []
        title = f"🔔 {s['name']} test"
        body = "Test notification from pushraker."
        dests: List[Tuple[str, str, Optional[str]]] = []
        btns = cfgmod.links(s, "test", env)
        if cfgmod.push_configured(s):
            target = {"ntfy": f"{s['server']}/{s['topic']}",
                      "gotify": s["gotify_server"],
                      "pushover": "Pushover",
                      "telegram": f"Telegram chat {s['telegram_chat']}",
                      "discord": "the Discord channel",
                      "discord_dm": f"Discord user {s['discord_user']}"}[
                          s["provider"]]
            text = body
            if s["provider"] == "telegram":
                text = body + "\n \n" + " · ".join(
                    f'<a href="{html.escape(u)}">{html.escape(t)}</a>'
                    for t, u in btns)
            elif s["provider"] in ("discord", "discord_dm") and discord:
                text = body + "\n\n" + " · ".join(
                    discord.link(t, u) for t, u in btns)
            elif s["provider"] == "gotify":
                text = body + "\n\n" + " · ".join(
                    f"[{t}]({u})" for t, u in btns)
            # Max priority: the test must come through however the phone's
            # notification settings treat lower priorities
            dests.append((target, cfgmod.row_url(s, "test", "max", env),
                          text.strip()))
        if cfgmod.email_configured(s):
            dests.append((f"email ({s['email_to']}) via {s['email_host']}",
                          cfgmod.email_url(s), None))
        if cfgmod.webhook_configured(s):
            dests.append(("the webhook", cfgmod.webhook_url(s, "test", env),
                          body))
        if not dests:
            raise cfgmod.ConfigError(
                "Set up a push service, email or a webhook first")
        if cfgmod.push_configured(s) and s["provider"] == "gotify":
            lines.append("Text only: picture attachments not supported by "
                         "Gotify.")
        cam = cfgmod.camera(s, env)
        path = None
        if cam and cam.get("snapshot"):
            path = await self.eventloop.run_in_thread(
                self._fetch_snapshot_sync, True, cam["snapshot"])
            lines.append("Attaching a camera snapshot" if path else
                         "Camera snapshot unavailable; sending without "
                         "an image")
        elif s["camera"]:
            lines.append(f'No webcam named "{s["camera"]}" in Moonraker; '
                         "sending without an image")
        failed = 0
        t0 = time.monotonic()
        try:
            for target, url, text in dests:
                if text is None and email_html is not None:
                    text = email_html.render(
                        title, body, btns, "info",
                        os.path.basename(path) if path else None,
                        datetime.datetime.now(self.tz), s["name"])
                ap = apprise.Apprise()
                if not ap.add(url):
                    lines.append(f"!! Could not build a notification "
                                 f"address for {target}")
                    failed += 1
                    continue
                for srv in ap:
                    srv.socket_connect_timeout = max(
                        srv.socket_connect_timeout, SEND_CONNECT_TIMEOUT)
                    srv.socket_read_timeout = max(
                        srv.socket_read_timeout, SEND_READ_TIMEOUT)
                channel = "email" if url.startswith("mailto") else (
                    "webhook" if url.startswith("json") else
                    url.split(":", 1)[0])
                self.apprise_log.take(channel)
                lines.append(f"Sending to {target}...")
                ok = await ap.async_notify(
                    title=title, body=text or body,
                    attach=None if url.startswith("gotify") else path)
                if not ok and path:
                    lines.append("Sending failed; retrying without the image")
                    retry = (email_html.without_image(text)
                             if text and text.startswith("<!doctype html>")
                             and email_html else
                             (text or body) + "\n(image could not be attached)")
                    ok = await ap.async_notify(title=title, body=retry)
                if ok:
                    lines.append(f"Sent to {target} "
                                 f"({time.monotonic() - t0:.0f} s).")
                else:
                    failed += 1
                    why = short_cause(self.apprise_log.take(channel))
                    lines.append(f"!! Not delivered to {target}"
                                 + (f" ({why})" if why else "") + ".")
                    if url.startswith("mailto"):
                        lines.extend(self._email_hint(why))
        finally:
            if path:
                shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        if failed:
            lines.append(f"!! {failed} of {len(dests)} test notifications "
                         "were not delivered.")
        else:
            lines.append("Test notification sent.")
        return {"ok": failed == 0, "lines": lines}


    # ---- notification data (templating.py) ----------------------------------

    def _data_for(self, rid: str) -> str:
        return self.data.get(DATA_ALIAS.get(rid, rid), "")

    @staticmethod
    def _ext_index(obj: str) -> Optional[int]:
        """extruder -> 0, extruder3 -> 3; None for anything else."""
        m = RX_EXTRUDER.fullmatch(obj)
        return int(m.group(1) or 0) if m else None

    @staticmethod
    def _ext_name(ch: int) -> str:
        return "extruder" if ch == 0 else f"extruder{ch}"

    async def _data_text(self, rid: str, text: Optional[str] = None,
                         sample: bool = False) -> str:
        """The row's data, rendered: one Moonraker query for every object
        the tokens read, the values pushraker adds for the rest. Never
        raises; what can't be read is "n/a". `sample`: the preview, with a
        sample print job when none is running."""
        text = self._data_for(rid) if text is None else text
        if not text.strip():
            return ""
        try:
            tokens = tp.parse(text)
            need = tp.objects_needed(tokens)
            state: Dict[str, Any] = {}
            if need:
                try:
                    state = await asyncio.wait_for(self._query(need),
                                                   DATA_QUERY_TIMEOUT)
                except Exception as e:
                    logging.warning("[pushraker] data query: %s", e)
            resolve = await self._resolver(tokens, state, sample)
            return tp.render(text, resolve)
        except Exception as e:
            logging.warning("[pushraker] notification data: %s", e)
            return ""

    async def _resolver(self, tokens: Sequence[tp.Token],
                        state: Dict[str, Any], sample: bool) -> Any:
        """Klipper's values from `state`; job.* and the values pushraker
        adds to an extruder (filament, slot) computed here."""
        job = self._job_values(sample)
        spools: Dict[int, Optional[Dict[str, Any]]] = {}
        for t in tokens:
            ch = self._ext_index(t.obj)
            if ch is not None and t.path[0] in EXTRUDER_ADDED and \
                    ch not in spools:
                sid = self._extruder_added(ch).get("spool_id")
                spools[ch] = await self._spool(int(sid)) \
                    if sid and self.spoolman_url else None

        def resolve(t: tp.Token) -> Any:
            if t.obj == tp.JOB:
                return job(t.path)
            ch = self._ext_index(t.obj)
            if ch is not None and t.path[0] in EXTRUDER_ADDED:
                return tp.dig(self._extruder_values(ch, spools.get(ch),
                                                    sample), t.path)
            return tp.dig(state.get(t.obj) or {}, t.path)
        return resolve

    def _extruder_added(self, ch: int) -> Dict[str, Any]:
        try:
            return self.printer.extruder_values(ch, self._printer_context())
        except Exception as e:
            logging.warning("[pushraker] extruder values: %s", e)
            return {}

    def _extruder_values(self, ch: int, spool: Optional[Dict[str, Any]],
                         sample: bool) -> Dict[str, Any]:
        """What pushraker adds to extruder<ch>: the filament loaded in it
        (from the printer, Spoolman's spool name and weight left) and, with
        an MMU, the slot feeding it. The preview fills what the printer
        doesn't know with sample values."""
        vals = self._extruder_added(ch)
        fil = dict(vals.get("filament") or {})
        if spool:
            fil["name"] = self._spool_name(spool) or fil.get("name")
            fil["grams_left"] = self._remaining(spool)
        out = {"filament": fil or None, "slot": vals.get("slot")}
        if sample and not fil:
            name, mat, vendor, colour, _, _ = \
                SAMPLE_FILAMENTS[ch % len(SAMPLE_FILAMENTS)]
            out["filament"] = {"name": name, "material": mat,
                               "vendor": vendor, "color": colour,
                               "grams_left": 640.}
        if sample and out["slot"] is None and self._ace_on:
            out["slot"] = f"ACE 1 slot {ch + 1}"
        return out

    def _printer_context(self) -> Dict[str, Any]:
        """What the harness sees when it adds its values."""
        return {"status": self._status,
                "ptc": self._status.get("print_task_config") or {},
                "map": self._job_map, "ace": self._ace() if self._ace_on
                else {}}

    def _job_values(self, sample: bool = False) -> Any:
        """A resolver for job.* paths: pushraker's values about the print
        job (not Klipper's). `sample`: the preview, with a sample Benchy
        when no print job is running."""
        active = self._job_active or self._busy()
        if active:
            base = self._job_live()
        elif sample:
            base = self._job_sample()
        else:
            base = {}

        def filament(n: int) -> Optional[Dict[str, Any]]:
            for f in base.get("filaments") or []:
                if f.get("index") == n - 1:
                    return f
            if sample and not active:
                return self._sample_filament(n)
            return None

        def value(path: Sequence[str]) -> Any:
            head, rest = path[0], list(path[1:])
            if head == "time" and rest == ["remaining"]:
                rest = ["remaining", "default"]
            if head == "filaments":
                fl = [f for f in base.get("filaments") or []
                      if f.get("used", True)]
                key = rest[0] if rest else "names"
                if key == "count":
                    return len(fl) if base else None
                if key == "total_grams":
                    g = [f["grams"] for f in fl if f.get("grams")]
                    return sum(g) if g else None
                field = {"names": "name", "materials": "material",
                         "colors": "color", "vendors": "vendor",
                         "grams": "grams"}.get(key)
                return [f.get(field) for f in fl] if field and fl else None
            m = re.fullmatch(r"filament(\d+)", head)
            if m:
                f = filament(int(m.group(1)))
                if f is None:
                    return None
                return tp.dig(f, rest) if rest else f.get("name")
            return tp.dig(base, [head] + rest)
        return value

    def _times(self, remaining: Dict[str, Optional[float]],
               default: Optional[float], elapsed: float) -> Dict[str, Any]:
        rem = {m: self._dur(v) for m, v in remaining.items()}
        rem["default"] = self._dur(default)
        return {"remaining": rem,
                "elapsed": self._dur(elapsed) if elapsed else None,
                "eta": self._eta(default)}

    def _eta(self, left: Optional[float]) -> Optional[tp.Clock]:
        if left is None:
            return None
        now = datetime.datetime.now(self.tz) if self.tz else \
            datetime.datetime.now()
        at = now + datetime.timedelta(seconds=max(0., left))
        return tp.Clock(at, (at.date() - now.date()).days)

    def _job_live(self) -> Dict[str, Any]:
        info = self._status["print_stats"].get("info") or {}
        pd = float(self._status["print_stats"].get("print_duration") or 0.)
        filename = self._job_filename
        fils = self._filaments()
        used = [f for f in fils if f.get("used")]
        return {
            "file": shown(filename), "progress": self._progress() * 100.,
            "layer": info.get("current_layer"),
            "layers": info.get("total_layer"),
            "plate": self._job_slicer.get("curr_bed_type") or None,
            "queue": self._queue_count(filename),
            "time": self._times(
                {m: self._left_seconds(m) for m in self._time_methods()},
                self._left_seconds(self._time_method()), pd),
            "filaments": fils,
            "extruders": sorted({f["extruder"] for f in used
                                 if f.get("extruder")},
                                key=lambda e: self._ext_index(e) or 0)
            or None,
        }

    def _sample_filament(self, n: int) -> Dict[str, Any]:
        name, mat, vendor, colour, nozzle, bed = \
            SAMPLE_FILAMENTS[(n - 1) % len(SAMPLE_FILAMENTS)]
        ch = (n - 1) % max(1, len(self.printer.extruders))
        return {"index": n - 1, "used": n == 1, "name": name,
                "material": mat, "vendor": vendor, "color": colour,
                "grams": 14.3 if n == 1 else round(2. + n * 0.7, 1),
                "extruder": self._ext_name(ch),
                "slot": f"ACE 1 slot {ch + 1}" if self._ace_on else None,
                "nozzle_temperature": float(nozzle),
                "nozzle_diameter": 0.4, "bed_temperature": float(bed)}

    def _job_sample(self) -> Dict[str, Any]:
        methods = self._time_methods()
        # Each method a little off the others, as on a real print
        default_m = self._time_method()
        others = sorted(m for m in methods if m != default_m)
        spread = {m: SAMPLE_LEFT + (i - 2 if i >= 2 else i - 3) * 60
                  for i, m in enumerate(others)}
        spread[default_m] = SAMPLE_LEFT
        default = SAMPLE_LEFT
        return {"file": "3DBenchy_PLA_1h12m", "progress": 42.3,
                "layer": 120, "layers": 285, "plate": "Textured PEI Plate",
                "queue": 0, "time": self._times(spread, default, 31 * 60),
                "filaments": [self._sample_filament(1)],
                "extruders": ["extruder"]}

    @staticmethod
    def _dur(seconds: Optional[float]) -> Optional[tp.Duration]:
        return tp.Duration(max(0., seconds)) if seconds is not None else None

    def _queue_count(self, filename: str) -> Optional[int]:
        try:
            jq = self.server.lookup_component("job_queue", None)
            files = [str(j) for j in list(getattr(jq, "queued_jobs", {})
                                          .values())] if jq else []
            return len([f for f in files if f != filename])
        except Exception:
            return None

    def _filaments(self) -> List[Dict[str, Any]]:
        """The job's filaments, in slicer order (index 0 = the slicer's
        filament 1), each as the slicer reported it, with the extruder
        that prints it. A slicer value is what was sliced with, not
        necessarily what runs: in a multi-material job one filament's bed
        temperature wins."""
        st = self._job_slicer
        ptc = self._job_ptc or {}
        used = set(self._job_tools)
        count = max([len(tp.split_values(st.get("filament_type", ""))),
                     len(self._job_weights)] + [t + 1 for t in used])
        ctx = dict(self._printer_context(), ptc=ptc)
        out = []
        for t in range(count):
            try:
                ch = self.printer.filament_extruder(t, ctx)
            except Exception:
                ch = t
            material = tp.per_filament(st, ("filament_type",), t) or (
                self._ptc_value(ptc, "filament_type", ch)
                if ch is not None else None)
            vendor = tp.per_filament(st, ("filament_vendor",), t) or (
                self._ptc_value(ptc, "filament_vendor", ch)
                if ch is not None else None)
            name = tp.per_filament(st, ("filament_settings_id",), t) or \
                " ".join(x for x in (vendor, material) if x) or None
            slot = None
            if ch is not None:
                slot = self._extruder_added(ch).get("slot")
            out.append({
                "index": t, "used": t in used or not used,
                "name": name, "material": material, "vendor": vendor,
                "color": tp.per_filament(st, ("filament_colour",
                                              "filament_color"), t),
                "grams": self._job_weights.get(t),
                "extruder": self._ext_name(ch) if ch is not None else None,
                "slot": slot,
                "nozzle_temperature": tp.number(tp.per_filament(
                    st, ("nozzle_temperature", "temperature"), t)),
                "nozzle_diameter": tp.number(tp.per_filament(
                    st, ("nozzle_diameter",), t)),
                "bed_temperature": tp.number(tp.per_filament(
                    st, tp.bed_temp_keys(st), t)),
            })
        return out

    @staticmethod
    def _ptc_value(ptc: Dict[str, Any], key: str, ch: int) -> Optional[str]:
        v = list(ptc.get(key) or [])
        val = str(v[ch]).strip() if 0 <= ch < len(v) else ""
        return val if val and val.upper() != "NONE" else None

    # -- the picker's tree --

    def _method_order(self) -> List[str]:
        """The time-left methods as the page lists them: the printer's
        default first, then alphabetically."""
        methods = self._time_methods()
        default = self.printer.time_default
        return [default] + sorted((m for m in methods if m != default),
                                  key=lambda m: methods[m][0].lower())

    async def _ep_objects(self, web_request: Any) -> Dict[str, Any]:
        """The picker's tree: the printer's objects by category, each
        entry named as Klipper (or pushraker) names it, with format
        examples from the printer's current values (and the preview's
        sample print job)."""
        kapis: APIComp = self.server.lookup_component("klippy_apis")
        try:
            objs = list(await kapis.get_object_list(default=[]))
        except Exception:
            objs = []
        extra_hidden = tuple(self.printer.hidden_objects)
        wanted = [o for o in objs if not tp.hidden(o, extra_hidden)
                  or o in self.printer.drivers or self._ext_index(o)
                  is not None]
        state: Dict[str, Any] = {}
        if wanted:
            try:
                state = await asyncio.wait_for(
                    self._query({o: None for o in wanted}),
                    DATA_QUERY_TIMEOUT)
            except Exception:
                state = {}
        hint = self.printer.hint
        placed: Set[str] = set()

        def fields(obj: str, spec: Sequence[Tuple[str, str, str, bool]]
                   ) -> List[Dict[str, Any]]:
            st = state.get(obj) or {}
            return [tp.node(f, hint=h, info=i, token=f"{obj}.{f}",
                            formats=None if f in tp.NO_FORMATS
                            else tp.format_options(st.get(f), frac))
                    for f, h, i, frac in spec
                    if st.get(f) is not None or not st]

        # Thermals
        thermals = []
        for obj in objs:
            kind = obj.partition(" ")[0]
            if kind in tp.THERMAL_TYPES and not tp.hidden(obj, extra_hidden):
                thermals.append(tp.node(
                    obj, hint=hint(obj) or tp.GENERIC_HINTS.get(obj, ""),
                    children=fields(obj, tp.THERMAL_TYPES[kind])))
                placed.add(obj)
        for obj, axis in self.printer.drivers.items():
            thermals.append(tp.node(obj, hint=f"{axis} driver", children=fields(
                obj, [("temperature", "now", "", False)])))
            placed.add(obj)
        # Toolheads: Klipper's extruders, and what pushraker adds to them
        heads = []
        exts = sorted((o for o in objs if self._ext_index(o) is not None),
                      key=lambda o: self._ext_index(o) or 0)
        for obj in exts:
            ch = self._ext_index(obj) or 0
            kids = fields(obj, tp.EXTRUDER_FIELDS)
            try:
                kids += self.printer.extruder_catalogue(
                    obj, bool(self.spoolman_url))
            except Exception as e:
                logging.warning("[pushraker] harness catalogue: %s", e)
            heads.append(tp.node(
                obj, info=f"Toolhead {ch + 1}. Klipper calls toolheads "
                "extruder, extruder1, extruder2…", children=kids))
            placed.add(obj)
        # Fans
        fans = []
        for obj in objs:
            kind = obj.partition(" ")[0]
            if kind in tp.FAN_TYPES and not tp.hidden(obj, extra_hidden):
                fans.append(tp.node(
                    obj, hint=hint(obj) or tp.GENERIC_HINTS.get(obj, ""),
                    children=fields(obj, tp.FAN_TYPES[kind])))
                placed.add(obj)
        try:
            for n in self.printer.fan_catalogue(state):
                fans.append(n)
                placed.add(n["name"])
        except Exception as e:
            logging.warning("[pushraker] harness catalogue: %s", e)
        # Other: everything else, as Klipper names it
        other = []
        for obj in objs:
            if obj in placed or tp.hidden(obj, extra_hidden):
                continue
            kids = tp.fields_tree(obj, state.get(obj) or {})
            if kids:
                other.append(tp.node(obj, hint=hint(obj) or "",
                                     children=kids))
        cats = [
            {"id": "thermals", "label": "🌡️ Thermals", "degree": True,
             "children": thermals},
            {"id": "toolheads", "label": "🔧 Toolheads",
             "info": "Klipper calls toolheads extruder, extruder1, "
             "extruder2… Values marked generated are pushraker's.",
             "children": heads},
            {"id": "fans", "label": "🌀 Fans", "children": fans},
            {"id": "job", "label": "🖨️ Print job",
             "info": "Generated by pushraker, not Klipper values.",
             "guide": "print-job-values", "children": self._job_tree()},
            {"id": "other", "label": "❔ Other", "children": other},
        ]
        return {"categories": [c for c in cats if c["children"]]}

    def _job_tree(self) -> List[Dict[str, Any]]:
        """job.*, with format examples from the current print job, or the
        preview's sample one."""
        job = self._job_values(sample=True)
        methods = self._time_methods()
        n = tp.node

        def leaf(path: str, hint: str = "", info: str = "",
                 frac: bool = False) -> Dict[str, Any]:
            v = job(path.replace("#", "1").split("."))
            last = path.rsplit(".", 1)[-1]
            return n(last, hint=hint, info=info, token=f"job.{path}",
                     formats=None if last in tp.NO_FORMATS
                     else tp.format_options(v, frac))

        default_label = methods[self._time_method()][0]
        remaining = [leaf("time.remaining.default", hint=default_label,
                          info="Uses the method selected in Print time "
                          "remaining estimation method (current: "
                          f"{default_label}).")]
        remaining += [leaf(f"time.remaining.{m}", info=methods[m][1])
                      for m in sorted(methods)]
        fil = [
            leaf("filament#.name"), leaf("filament#.material"),
            leaf("filament#.color"), leaf("filament#.vendor"),
            leaf("filament#.grams", info="Filament the slicer says this "
                 "filament uses."),
            leaf("filament#.extruder", info="The toolhead it prints with, "
                 "by Klipper's name (extruder, extruder1…)."),
        ] + ([leaf("filament#.slot", info="With an MMU (multiACE): the "
                    "slot feeding its toolhead.")] if self._ace_on else []) + [
            leaf("filament#.nozzle_temperature", info="As sliced. The "
                 "temperature in effect is whatever the G-code sets."),
            leaf("filament#.nozzle_diameter", info="As sliced."),
            leaf("filament#.bed_temperature", info="As sliced for the "
                 "plate type. In a multi-material print one filament's bed "
                 "temperature wins, so the heated bed may run at another."),
        ]
        for f in fil:
            f["placeholder"] = True      # the user types the number
        return [
            leaf("file"),
            leaf("progress", info="Through the G-code file, 0-100."),
            leaf("layer"), leaf("layers"),
            leaf("plate", hint="plate type", info="As sliced (OrcaSlicer "
                 "and its forks)."),
            leaf("queue", info="Print jobs waiting in Moonraker's job "
                 "queue after this one."),
            n("time", children=[
                n("remaining", children=remaining),
                leaf("time.elapsed", info="Print time so far (stops while "
                     "paused)."),
                leaf("time.eta", info="When the print job should finish, by "
                     "the selected method, in your time zone; +1d when "
                     "it's tomorrow."),
            ]),
            n("filaments", hint="all filaments", children=[
                leaf("filaments.names"), leaf("filaments.materials"),
                leaf("filaments.colors"), leaf("filaments.count"),
                leaf("filaments.grams"),
                leaf("filaments.total_grams")]),
            n("filament#", hint="specific filament",
              info="One of the job's filaments, by the slicer's number (1, "
              "2, 3…). Clicking a value inserts it with # selected: type "
              "the number.", children=fil),
            leaf("extruders", hint="toolheads this print job uses",
                 info="By Klipper's name (extruder, extruder1…)."),
        ]

    async def _ep_preview(self, web_request: Any) -> Dict[str, Any]:
        """The page's Preview: the text rendered with the printer's live
        values, and a sample print job when none is running."""
        args = dict(web_request.get_args())
        text = cfgmod.clean_text(args.get("text", ""))
        rid = str(args.get("row") or "started")
        rendered = await self._data_text(rid, text, sample=True)
        return {"text": rendered, "live_job": self._job_active or self._busy(),
                "placeholders": tp.placeholders(text)}

    @staticmethod
    def _email_hint(why: str) -> List[str]:
        text = why or ""
        if "Unauthorized IP" in text or "Unauthorised IP" in text:
            return ["The mail server doesn't accept connections from this "
                    "network's IP address. On Brevo, allow it, or turn off "
                    "IP blocking, under Security > Authorised IPs."]
        if "535" in text or "534" in text or "authentication" in text.lower():
            return ["The mail server rejected the username or password. "
                    "Gmail needs an app password; Brevo needs an SMTP key "
                    "(not your account password)."]
        return []

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _option(config: ConfigHelper, key: str) -> Optional[str]:
        """An option as a template: the URLs refer to their tokens and
        passwords as {secrets.notifications.<name>}, kept in Moonraker's
        secrets file (never served or logged) by notifications-config.py."""
        if config.get(key, None) is None:
            return None
        try:
            return str(config.gettemplate(key).render()).strip()
        except Exception as e:
            raise config.error(f"[{config.get_name()}] {key}: {e}")


    def _spawn(self, coro: Awaitable) -> None:
        task = self.eventloop.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logging.error("[pushraker] task failed: %s",
                              t.exception(), exc_info=t.exception())
        task.add_done_callback(_done)

    async def _query(self, objects: Dict[str, Any]) -> Dict[str, Any]:
        kapis: APIComp = self.server.lookup_component("klippy_apis")
        try:
            return await kapis.query_objects(objects, default={})
        except Exception as e:
            logging.error("[pushraker] state query failed: %s", e)
            return {}

    def _klippy_shutdown(self) -> bool:
        try:
            kconn = self.server.lookup_component("klippy_connection")
            return kconn.state == KlippyState.SHUTDOWN
        except Exception:
            return False

    def _live_fault(self, ps: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The fault in print_stats, unless it's the one a resume already
        put behind us (Klipper doesn't clear it on RESUME)."""
        ex = coded(ps)
        if ex is not None and fault_key(ex) == self._stale_fault:
            return None
        return ex

    def _fault_recent(self, key: FaultKey) -> bool:
        last = self._faults_sent.get(key)
        return last is not None and \
            time.monotonic() - last < self.repeat_interval

    def _mark_fault(self, key: FaultKey) -> None:
        self._faults_sent[key] = time.monotonic()
        self._last_fault_send = time.monotonic()

    def _fault_row(self) -> Optional[str]:
        """Where a fault that ended a print is reported."""
        if "error" in self.rows:
            return "error"
        if "cancelled" in self.rows:
            return "cancelled"
        return None

    def _fetch_snapshot_sync(self, quiet: bool = False,
                             url: Optional[str] = None) -> Optional[str]:
        url = url or self.snapshot_url
        if not url:
            return None
        try:
            with urllib.request.urlopen(url, timeout=SNAPSHOT_TIMEOUT) as resp:
                data = resp.read(SNAPSHOT_MAX + 1)
        except Exception as e:
            if not quiet:
                logging.warning("[pushraker] no snapshot: %s", e)
            return None
        if not data.startswith(b"\xff\xd8") or len(data) > SNAPSHOT_MAX:
            logging.warning("[pushraker] snapshot is not a JPEG")
            return None
        tmpdir = tempfile.mkdtemp(prefix="pushraker_")
        path = os.path.join(tmpdir, "snapshot.jpg")
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            logging.warning("[pushraker] snapshot not saved: %s", e)
            return None
        return path

    async def _fetch_snapshot(self, quiet: bool = False) -> Optional[str]:
        return await self.eventloop.run_in_thread(
            self._fetch_snapshot_sync, quiet)

    async def _wait_for_snapshot(self, timeout: float) -> Optional[str]:
        """Snapshot, waiting up to `timeout` for the camera to come up."""
        deadline = time.monotonic() + timeout
        while True:
            last = time.monotonic() + PL_CAMERA_POLL >= deadline
            path = await self._fetch_snapshot(quiet=not last)
            if path is not None or last:
                return path
            await asyncio.sleep(PL_CAMERA_POLL)

    def _thumbnail(self, filename: str) -> Optional[str]:
        try:
            fm = self.server.lookup_component("file_manager")
            meta = fm.get_metadata_storage().get(filename, {}) or {}
            root = fm.get_directory("gcodes")
        except Exception as e:
            logging.warning("[pushraker] metadata lookup failed: %s",
                            e)
            return None
        thumbs = [t for t in meta.get("thumbnails") or []
                  if t.get("relative_path")]
        if not thumbs:
            return None
        best = max(thumbs, key=lambda t: t.get("width", 0) or 0)
        rel = best["relative_path"]
        for path in (os.path.join(root, os.path.dirname(filename), rel),
                     os.path.join(root, rel)):
            if os.path.isfile(path):
                return path
        return None

    async def _send(
        self, rid: str, title: str, body: str,
        filename: str = "", snapshot: Optional[str] = None,
        snapshot_ready: bool = False, delay: float = 0.,
        camera_wait: float = 0.,
    ) -> bool:
        """Send one notification to each of its destinations; images are
        optional and never block it.

        `delay` postpones the snapshot (e.g. until the head has moved away
        after a pause). Each channel sends one notification at a time, in
        the order they happened, so one that is retrying or delayed is not
        overtaken (e.g. resumed arriving before paused). `camera_wait`
        gives the camera that long to come up (e.g. right after boot).
        """
        row = self.rows.get(rid)
        if row is None:
            return False
        image, dests = row
        # Take a place in each channel's queue now, before any waiting
        tickets = [self._reserve(d.channel, rid) for d in dests]
        try:
            if delay:
                await asyncio.sleep(delay)
            body = self._lines(body, await self._data_text(rid))
            if camera_wait and image == "snapshot" and not snapshot_ready:
                path = await self._wait_for_snapshot(camera_wait)
            else:
                path = await self._image(image, filename, snapshot,
                                         snapshot_ready)
            named = self._named_copy(path, title, filename)
            report: List[Dict[str, Any]] = []
            try:
                results = self._outcomes(rid, dests, await asyncio.gather(*(
                    self._deliver(rid, d, t, [(title, body, named)], report)
                    for d, t in zip(dests, tickets)), return_exceptions=True))
                self._console_report(report)
            finally:
                if named is not None and named != path:
                    shutil.rmtree(os.path.dirname(named), ignore_errors=True)
                if path is not None and image == "snapshot":
                    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        finally:
            for t in tickets:
                await t.gate.done(t.ticket)
        return all(results)

    @staticmethod
    def _outcomes(rid: str, dests: List[Destination],
                  results: List[Any]) -> List[bool]:
        """gather(return_exceptions=True) hands back exception objects,
        which are truthy: a delivery that crashed is logged and counts as
        failed, not sent."""
        out = []
        for d, r in zip(dests, results):
            if isinstance(r, BaseException):
                logging.error("[pushraker] %s (%s): send crashed: "
                              "%r", rid, d.channel, r, exc_info=r)
                out.append(False)
            else:
                out.append(bool(r))
        return out

    def _image_name(self, title: str, filename: str, ext: str) -> str:
        """File name for a notification's picture, so saved or exported
        images say what they were: time, printer, notification, file, e.g.
        2026-10-22_17-05-03_U1-martha_50pct_Benchy.jpg. Letters, digits,
        . _ - only (it is also the email's inline image reference)."""
        now = datetime.datetime.now(self.tz) if self.tz else \
            datetime.datetime.now()
        # The notification as its title says it, without the emoji and the
        # printer's name ("⏳ U1-martha 50%" -> "50%")
        event = title.split(" ", 1)[1] if " " in title else title
        if event.startswith(self.name + " "):
            event = event[len(self.name) + 1:]
        event = event.replace("%", "pct")

        def clean(text: str, cap: int) -> str:
            text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
            return text[:cap].rstrip("-.")
        parts = [now.strftime("%Y-%m-%d_%H-%M-%S"), clean(self.name, 32),
                 clean(event, 48), clean(shown(filename), 64)]
        return "_".join(p for p in parts if p) + ext

    def _named_copy(
        self, path: Optional[str], title: str, filename: str = ""
    ) -> Optional[str]:
        """A copy of the picture under its descriptive name, in its own
        temporary folder (the original may be a Moonraker thumbnail, or
        shared with another notification). The original on failure."""
        if path is None:
            return None
        try:
            name = self._image_name(title, filename or self._job_filename,
                                    os.path.splitext(path)[1] or ".jpg")
            tmpdir = tempfile.mkdtemp(prefix="pushraker_")
            dest = os.path.join(tmpdir, name)
            shutil.copyfile(path, dest)
            return dest
        except Exception as e:
            logging.warning("[pushraker] image name: %s", e)
            return path

    def _reserve(self, channel: str, rid: str) -> Ticket:
        """Place in a channel's send order, taken when the notification
        happens."""
        gate = self._gates.setdefault(channel, ChannelGate())
        return Ticket(gate, gate.ticket(rid in URGENT_ROWS), time.monotonic())

    def _prepare(
        self, rid: str, dest: Destination, title: str, body: str,
        path: Optional[str]
    ) -> Tuple[str, str, Dict[str, str]]:
        """Title, text and (email) headers for one destination: the same for
        every kind of notification. Email: HTML card and thread headers;
        Telegram: escaped HTML and named links; Discord: escaped Markdown and
        links."""
        if dest.email:
            return self._email_prepare(rid, title, body, path)
        body = self._fit_body(dest.channel, body)
        if dest.channel == "telegram":
            title, body = self._telegram_prepare(rid, title, body)
        elif dest.channel == "gotify":
            body = self._gotify_markdown(body.strip() or title)
            links = " · ".join(f"[{self._md_escape(t)}]({u})"
                               for t, u in self._links(rid))
            if links:
                body += "\n\n" + links
        elif dest.channel == "discord" and discord is not None:
            body = discord.escape(body.strip() or title)
            links = " · ".join(discord.link(t, u) for t, u in self._links(rid))
            if links:
                body += "\n\n" + links
        return title, body, {}

    def _fit_body(self, channel: str, body: str) -> str:
        """Cut a body (with its data) to the service's limit, leaving room
        for the links some services get in the text."""
        limit = BODY_LIMITS.get(channel)
        if limit is None or self._fits(channel, body):
            return body
        chars, n = limit
        room = max(64, n - 200)
        if chars:
            return body[:room - 1].rstrip() + "…"
        return body.encode()[:room - 3].decode("utf-8", "ignore").rstrip() + "…"

    @staticmethod
    def _md_escape(text: str) -> str:
        return re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", text)

    def _gotify_markdown(self, text: str) -> str:
        """Gotify shows Markdown: escape it, and keep line breaks (a single
        newline is a space in Markdown)."""
        return "\n\n".join(
            "  \n".join(self._md_escape(line) for line in para.split("\n"))
            for para in text.split("\n\n"))

    def _email_prepare(
        self, rid: str, title: str, body: str, path: Optional[str] = None
    ) -> Tuple[str, str, Dict[str, str]]:
        """Emails about one print go in one thread: the same subject
        (printer and file name; the notification's title moves into the
        email) and References to the same thread id. Others keep their
        title as subject. Sent as an HTML card (email_html)
        with the image inline. The headers are set right before each attempt
        (the channel sends one at a time)."""
        thread = self._thread_for(rid)
        headers: Dict[str, str] = {}
        # Gmail hides text a thread already showed ("•••"), so a repeated
        # notification (same fault twice) would look empty: the time at the
        # top and bottom keeps every email distinct.
        now = datetime.datetime.now(self.tz) if self.tz else \
            datetime.datetime.now()
        subject = title
        if thread is not None:
            subject = f"{self.name}: {shown(thread['file'])}"
            ref = f"<{thread['id']}@push-notifications.u1>"
            headers = {"In-Reply-To": ref, "References": ref}
        text = body.strip() or title
        if email_html is not None:
            page = email_html.render(
                title, text, self._links(rid), NOTIFY_TYPES.get(rid, "info"),
                os.path.basename(path) if path else None, now, self.name)
            return subject, page, headers
        body = self._lines(text, "", *(f"{t}: {u}" for t, u in
                                       self._links(rid)))
        if thread is not None:
            body = self._lines(f"{title} · {now:%H:%M}", "", body, "",
                               f"Sent {now:%Y-%m-%d %H:%M:%S}")
        return subject, body, headers

    def _thread_for(self, rid: str) -> Optional[Dict[str, str]]:
        self._low_load()          # restores the thread after a restart
        th = self._thread
        if th is None:
            return None
        if rid in THREAD_ROWS:
            return th
        if rid == "power_loss":
            return th if self._pl_file and \
                self._pl_file == os.path.basename(th["file"]) else None
        # Faults: while printing, or right as the print ended
        if self._job_active or (
                time.monotonic() - self._job_ended < THREAD_FAULT_WINDOW):
            return th
        return None

    def _thread_start(self, filename: str, recovered: bool) -> None:
        self._low_load()
        if recovered and self._thread and self._thread["file"] == filename:
            return              # power-loss recovery: same thread
        self._thread = {"id": f"{int(time.time())}.{os.getpid()}."
                              f"{abs(hash(filename)) % 100000}",
                        "file": filename}
        self._low_save()

    def _telegram_prepare(
        self, rid: str, title: str, body: str
    ) -> Tuple[str, str]:
        """Telegram is sent as HTML: the text is escaped, and the links
        are added as named links. Apprise joins blank lines in HTML, so a
        blank line is kept as a line with a space."""
        def esc(t: str) -> str:
            return html.escape(t, quote=False)
        text = esc(body.strip() or title)
        links = " · ".join(f'<a href="{html.escape(u)}">{esc(t)}</a>'
                           for t, u in self._links(rid))
        if links:
            text += "\n\n" + links
        return esc(title), text.replace("\n\n", "\n \n")

    def _links(self, rid: str) -> List[Tuple[str, str]]:
        """(name, url) of the links added to emails and Telegram."""
        out = []
        if self.web_ui_url:
            out.append(("Web UI", self.web_ui_url))
        if self.camera_url:
            out.append(("Live Camera", self.camera_url))
        if rid in FAULT_ROWS and self.error_index_url:
            out.append(("Error Index", self.error_index_url))
        return out

    async def _image(
        self, image: str, filename: str, snapshot: Optional[str],
        snapshot_ready: bool
    ) -> Optional[str]:
        if image == "snapshot":
            return snapshot if snapshot_ready else await self._fetch_snapshot()
        if image == "render" and filename:
            return self._thumbnail(filename)
        return None

    async def _wait_network(self, rid: str, title: str = "") -> None:
        if has_network():
            return
        logging.info("[pushraker] %s: no network, waiting up to "
                     "%ds", rid, NETWORK_WAIT)
        if title:
            self._console_join(("wait", title), "")
        deadline = time.monotonic() + NETWORK_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(NETWORK_POLL)
            if has_network():
                logging.info("[pushraker] %s: network is up", rid)
                return
        logging.warning("[pushraker] %s: still no network, "
                        "trying anyway", rid)

    async def _deliver(
        self, rid: str, dest: Destination, t: Ticket,
        msgs: List[Tuple[str, str, Optional[str]]],
        report: Optional[List[Dict[str, Any]]] = None
    ) -> bool:
        """Send one or more messages (title, text, image) to one
        destination, in its channel's turn. Doesn't remove the images
        (shared by destinations). Each outcome goes in `report`, for the
        console."""
        ok = True
        for shown_title, body, path in msgs:
            title, body, headers = self._prepare(rid, dest, shown_title,
                                                 body, path)
            result: Dict[str, Any] = dict(title=shown_title,
                                          channel=dest.channel)
            try:
                ok = await self._attempts(rid, dest, t, title, body, headers,
                                          path, result) and ok
            except Exception as e:
                # Reported on the console as a failure too (the caller
                # logs it with its traceback)
                result.update(ok=False, cause=f"error: {type(e).__name__}",
                              attempts=result.get("attempts")
                              or 1 + len(SEND_RETRY_DELAYS))
                if report is not None:
                    report.append(result)
                raise
            if report is not None:
                report.append(result)
        return ok

    # -- console --

    def _console(self, line: str) -> None:
        try:
            self.server.send_event("server:gcode_response", line)
        except Exception as e:
            logging.debug("[pushraker] console: %s", e)

    @staticmethod
    def _services(channels: List[str]) -> str:
        seen = sorted(set(channels), key=lambda c: CHANNEL_ORDER.index(c)
                      if c in CHANNEL_ORDER else len(CHANNEL_ORDER))
        return ", ".join(CHANNEL_NAMES.get(c, c) for c in seen)

    def _console_join(self, key: Tuple[Any, ...], channel: str) -> None:
        """A line that others like it (same notification, cause, attempt)
        may join for CONSOLE_GROUP seconds before it's printed."""
        first = key not in self._console_pending
        self._console_pending.setdefault(key, []).append(channel)
        if first:
            self._spawn(self._console_flush(key))

    async def _console_flush(self, key: Tuple[Any, ...]) -> None:
        await asyncio.sleep(CONSOLE_GROUP)
        channels = self._console_pending.pop(key, [])
        if key[0] == "wait":
            self._console(f"Waiting up to {NETWORK_WAIT / 60:.0f} minutes "
                          f"for network to return, to send notification "
                          f"\"{key[1]}\"")
            return
        _kind, title, cause, attempt, attempts, retry = key
        self._console(f"!! Failed to send notification \"{title}\" to "
                      f"{self._services(channels)} ({cause}), attempt "
                      f"{attempt}/{attempts}, will retry in {retry} seconds")

    def _console_report(self, report: List[Dict[str, Any]]) -> None:
        """After a notification's sends: what went where, then what
        failed for good (one line per cause)."""
        titles: List[str] = []
        for r in report:
            if r["title"] not in titles:
                titles.append(r["title"])
        for title in titles:
            mine = [r for r in report if r["title"] == title]
            sent = [r for r in mine if r.get("ok")]
            if sent:
                pic = [r["channel"] for r in sent if r.get("picture")]
                text = [r["channel"] for r in sent if not r.get("picture")]
                parts = []
                if pic:
                    parts.append(f"{self._services(pic)}, with picture")
                if text:
                    parts.append(self._services(text))
                self._console(f"Sent notification \"{title}\" to "
                              + ", and to ".join(parts))
            causes: Dict[Tuple[str, int], List[str]] = {}
            for r in mine:
                if not r.get("ok") and r.get("attempts"):
                    causes.setdefault((r.get("cause") or "failed",
                                       r["attempts"]), []).append(r["channel"])
            for (cause, n), channels in causes.items():
                self._console(f"!! Failed to send notification \"{title}\" "
                              f"to {self._services(channels)} ({cause}), "
                              f"attempt {n}/{n}")

    def _expired(self, rid: str, t: Ticket) -> bool:
        return rid in EXPIRING_ROWS and \
            time.monotonic() - t.created > STATUS_EXPIRY

    async def _attempts(
        self, rid: str, dest: Destination, t: Ticket, title: str, body: str,
        headers: Dict[str, str], path: Optional[str],
        result: Optional[Dict[str, Any]] = None
    ) -> bool:
        """One message with retries. The channel is busy only during an
        attempt, but this notification's ticket keeps the channel's later
        (non-fault) notifications waiting until it is sent or given up,
        so they arrive in order; a fault can go ahead between attempts."""
        what = f"{rid} ({dest.channel})"
        ap = dest.ap
        body = body.strip() or title
        if dest.channel == "gotify":
            path = None         # Gotify messages can't carry attachments
        ok = False
        attempts = 1 + len(SEND_RETRY_DELAYS)
        kind = NOTIFY_TYPES.get(rid, "info")
        result = {} if result is None else result
        shown_title = result.get("title", title)
        pictured = False
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(SEND_RETRY_DELAYS[attempt - 1])
            await self._wait_network(what, shown_title)
            await t.gate.acquire(t.ticket)
            try:
                if self._expired(rid, t):
                    self._skip(what, "too old (sent more than %d min after "
                               "it happened)" % (STATUS_EXPIRY // 60))
                    return False
                gap = SEND_GAP.get(dest.channel, 0.)
                if gap:
                    # Holding the channel: the next one waits behind this
                    wait = t.gate.last_sent + gap - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                if dest.email:
                    # Thread headers for email only: a webhook's own
                    # +Header parameters would be wiped
                    for srv in ap:
                        if hasattr(srv, "headers"):
                            srv.headers = dict(headers)
                # Each attempt waits for the server's answer before the next
                # is scheduled. The last one is text only, in case the image
                # is what the server rejects (e.g. no attachments allowed).
                self.apprise_log.take(dest.channel)
                if attempt < attempts - 1 or path is None:
                    pictured = path is not None
                    ok = await ap.async_notify(
                        body=body, title=title, attach=path, notify_type=kind)
                else:
                    pictured = False
                    if email_html is not None and email_html.is_page(body):
                        last = email_html.without_image(body)
                    else:
                        last = body + "\n(image could not be attached)"
                    ok = await ap.async_notify(
                        body=last, title=title, notify_type=kind)
            finally:
                t.gate.last_sent = time.monotonic()
                await t.gate.release(t.ticket)
            if ok:
                break
            cause = short_cause(self.apprise_log.take(dest.channel))
            result.update(cause=cause, attempts=attempts)
            if attempt < attempts - 1:
                self._console_join(
                    ("fail", shown_title, cause, attempt + 1, attempts,
                     SEND_RETRY_DELAYS[attempt]), dest.channel)
                logging.warning(
                    "[pushraker] %s: attempt %d failed, "
                    "retrying in %ds%s", what, attempt + 1,
                    SEND_RETRY_DELAYS[attempt],
                    " without the image"
                    if attempt == attempts - 2 and path is not None
                    else "")
        result.update(ok=bool(ok), picture=bool(ok) and pictured)
        if ok:
            logging.info("[pushraker] %s: sent", what)
        else:
            logging.error("[pushraker] %s: send failed after %d "
                          "attempts", what, attempts)
        return bool(ok)

    @staticmethod
    def _skip(what: str, why: str) -> None:
        logging.info("[pushraker] %s: not sent, %s", what, why)

    def _clock(self, seconds_from_now: float) -> str:
        if self.tz is None:
            return ""
        now = datetime.datetime.now(self.tz)
        at = now + datetime.timedelta(seconds=seconds_from_now)
        days = (at.date() - now.date()).days
        return at.strftime("%H:%M") if days == 0 else \
            f"+{days}d {at.strftime('%H:%M')}"

    # ---- time left -------------------------------------------------------

    def _time_method(self) -> str:
        """The method in use: the setting, else the printer's default."""
        m = {"first_layer": "slicer_first_layer"}.get(
            self.time_left_method, self.time_left_method)
        return m if m in self._time_methods() else self.printer.time_default

    def _time_methods(self) -> Dict[str, Tuple[str, str]]:
        """id -> (label, help), the generic ones and the printer's own."""
        out = dict(TIME_METHODS)
        out.update(self.printer.time_methods)
        return out

    def _left_seconds(self, method: str) -> Optional[float]:
        """Seconds of printing left by one method, None when it can't be
        computed now (no estimate, nothing printed yet, done).

        slicer       the slicer's estimate minus the print time so far
        first_layer  the same, counting from when layer 1 began (the
                     pre-print routine isn't in the slicer's estimate)
        file         print time so far / file progress, minus print time
                     (Fluidd's and Mainsail's "file" estimate)
        filament     the same by filament used / filament total
        blend        KlipperScreen's "auto": the slicer (from the first
                     layer) until 30 % of the file, then a weighted mix of
                     slicer, filament and file, leaning on file as the
                     print goes on
        m73          the slicer's own M73 R markers in the file, at the
                     current position (interpolated); blend without them
        The harness may add methods (Printer.time_methods / left_seconds).
        """
        ps = self._status["print_stats"]
        pd = float(ps.get("print_duration") or 0.)
        p = self._progress()
        if p >= 1.:
            return None
        est = self._estimated_time(self._job_filename)
        pd1 = pd - (self._layer1_pd or 0.)     # print time since layer 1
        if method == "slicer":
            return est - pd if est > 0 else None
        if method == "slicer_first_layer":
            return est - pd1 if est > 0 else None
        if method == "file":
            return pd / p - pd if pd > 0 and p > 0 else None
        if method == "filament":
            used = float(ps.get("filament_used") or 0.)
            total = self._filament_total(self._job_filename)
            return pd / (used / total) - pd \
                if pd > 0 and total > 0 and 0 < used < total else None
        if method == "m73":
            left = self._m73_left()
            return left if left is not None else self._left_seconds("blend")
        if method == "blend":
            if est > 0 and p <= BLEND_ABOVE:
                left = est - pd1
                # Already past the slicer's estimate: the pace is all
                # there is
                return left if left > 0 or not (pd > 0 and p > 0) \
                    else pd / p - pd
            total_file = pd / p if pd > 0 and p > 0 else 0.
            used = float(ps.get("filament_used") or 0.)
            ftot = self._filament_total(self._job_filename)
            total_fil = pd / (used / ftot) if pd > 0 and ftot > 0 and \
                0 < used < ftot else 0.
            if not total_file:
                return est - pd1 if est > 0 else None
            # The slicer's total, placed on the first-layer clock
            slicer_total = est + (self._layer1_pd or 0.)
            w_s = 1. + BLEND_ABOVE - p if est > 0 and pd < slicer_total \
                else 0.
            w_f = min(p - BLEND_ABOVE, 0.33) if total_fil and pd < total_fil \
                else 0.
            w_p = p - BLEND_ABOVE
            total = (slicer_total * w_s + total_fil * w_f + total_file * w_p) \
                / (w_s + w_f + w_p)
            return total - pd
        try:
            return self.printer.left_seconds(method, {
                "estimated": est, "print_duration": pd, "progress": p,
                "sd_progress": float(
                    self._status["virtual_sdcard"].get("progress") or 0.),
                "layer1_pd": self._layer1_pd})
        except Exception as e:
            logging.warning("[pushraker] time left (%s): %s", method, e)
            return None

    def _m73_left(self) -> Optional[float]:
        """Minutes-left markers around the current file position, read as
        seconds (interpolated between the marker before and after)."""
        marks = self._job_m73
        pos = self._status["virtual_sdcard"].get("file_position")
        if not marks or pos is None:
            return None
        pos = int(pos)
        before = None
        for off, mins in marks:
            if off <= pos:
                before = (off, mins)
            else:
                if before is None:
                    return mins * 60.
                frac = (pos - before[0]) / max(1, off - before[0])
                return (before[1] + (mins - before[1]) * frac) * 60.
        return before[1] * 60. if before else None

    def _filament_total(self, filename: str) -> float:
        try:
            fm = self.server.lookup_component("file_manager")
            meta = fm.get_metadata_storage().get(filename, {}) or {}
            return float(meta.get("filament_total") or 0.)
        except Exception:
            return 0.

    def _time_left(self, method: Optional[str] = None) -> str:
        """"~41m left, ETA 21:52" by the method in use (or `method`); ""
        when nothing can be said yet."""
        left = self._left_seconds(method or self._time_method())
        if left is None:
            return ""
        left = max(0., left)
        clock = self._clock(left)
        # "~": time left is an estimate (the slicer estimate is labelled)
        return f"~{format_duration(left)} left" + (f", ETA {clock}" if clock
                                                   else "")

    def _estimated_time(self, filename: str) -> float:
        try:
            fm = self.server.lookup_component("file_manager")
            meta = fm.get_metadata_storage().get(filename, {}) or {}
            return float(meta.get("estimated_time") or 0.)
        except Exception:
            return 0.

    def _slicer_estimate(self, filename: str) -> str:
        est = self._estimated_time(filename)
        if est <= 0.:
            return ""
        clock = self._clock(est)
        return f"Estimated print time {format_duration(est)}" + (
            f", ETA {clock}" if clock else "")

    @staticmethod
    def _lines(*parts: str) -> str:
        return "\n".join(p for p in parts if p)

    def _title(self, key: str, **kw: Any) -> str:
        return TITLES[key].format(name=self.name, **kw)

    # ---- job events ----------------------------------------------------

    async def _on_job_event(
        self, job_event: JobEvent, prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any]
    ) -> None:
        ps = new_stats
        filename = ps.get("filename") or ""
        ex = self._live_fault(ps)
        if job_event == JobEvent.STARTED:
            self._stale_fault = None
            self._job_gen += 1
            gen = self._job_gen
            progress = await self._start_job(filename)
            if gen != self._job_gen:
                self._job_active = False    # it ended while being seeded
                return
            # Snapmaker resumes a power-loss print as a new job, part-way
            # into the file with its print time kept. A small file's start
            # G-code alone is past 1 %, so the print time decides there.
            recovered = bool(self.printer.power_loss_env_paths) and \
                progress >= RECOVERY_MIN_PROGRESS and (
                    float(ps.get("print_duration") or 0.) > RECOVERY_MIN_TIME
                    or progress >= RECOVERY_SURE_PROGRESS)
            self._thread_start(filename, recovered)
            self._preprint_seed(recovered)
            if recovered:
                # Snapmaker resumes a power-loss print as a new job
                self._spawn(self._send(
                    "resumed", self._title("recovered"),
                    self._lines(shown(filename), self._time_left())))
            else:
                self._spawn(self._send(
                    "started", self._title("started"),
                    self._lines(shown(filename),
                                self._slicer_estimate(filename),
                                self._queue_line(filename, starting=True),
                                self._pauses_line(),
                                self._preprint_line()),
                    filename=filename))
            # After the started notification has taken its place in the
            # queues, so filament warnings always arrive after it
            self._start_filament_watch(filename, progress, check_start=True)
        elif job_event == JobEvent.PAUSED:
            if ex is not None and "error" in self.rows and \
                    self._refill_expected(ex):
                self._mark_fault(fault_key(ex))
                self._spawn(self._runout_hold(filename, ex))
            elif ex is not None and "error" in self.rows:
                self._mark_fault(fault_key(ex))
                self._spawn(self._send(
                    "error", self._title("error", kind="paused"),
                    f"{shown(filename)}\n{describe(ex)}",
                    delay=PAUSE_SNAPSHOT_DELAY))
            elif "paused" in self.rows and self._job_pauses:
                self._spawn(self._file_pause_alert(filename))
            else:
                self._spawn(self._send(
                    "paused", self._title("paused"), shown(filename),
                    delay=PAUSE_SNAPSHOT_DELAY))
        elif job_event == JobEvent.RESUMED:
            self._driver_hot_sent = False   # a hot driver may warn again
            if ex is not None:
                self._stale_fault = fault_key(ex)
            self._status["print_stats"].update(
                {k: ps[k] for k in ("print_duration",) if k in ps})
            self._spawn(self._send(
                "resumed", self._title("resumed"),
                self._lines(shown(filename), self._time_left())))
        elif job_event == JobEvent.ERROR:
            # A Klipper shutdown ends the job with an error too: that goes
            # to the shutdown row (the exception handler would otherwise
            # find it already reported here)
            shutdown = self._klippy_shutdown()
            rid = "shutdown" if shutdown else "error"
            if self._job_done_sent or \
                    (ex is not None and self._fault_recent(fault_key(ex))):
                self._skip(rid, "already reported")
            elif rid in self.rows:
                if ex is not None:
                    self._mark_fault(fault_key(ex))
                    detail = f"{describe(ex)}"
                else:
                    detail = ps.get("message") or "Print failed"
                if shutdown:
                    self._last_fault_send = time.monotonic()
                self._spawn(self._send(
                    rid, self._title("shutdown") if shutdown
                    else self._title("error", kind="error"),
                    f"{shown(filename)}\n{detail}"))
        elif job_event in (JobEvent.COMPLETE, JobEvent.CANCELLED):
            if self._job_done_sent:
                self._skip(str(job_event), "already reported by the print hook")
            else:
                # The print hooks did not report it (e.g. no PRINT_END)
                logging.info("[pushraker] %s: no print hook "
                             "report, sending from the job event", job_event)
                self._spawn(self._report_end(
                    "complete" if job_event == JobEvent.COMPLETE
                    else "cancelled", ps))
        if job_event.finished:
            self._job_active = False
            self._job_ended = time.monotonic()
            self._job_gen += 1
            self._filament_job += 1

    async def _start_job(self, filename: str, query: bool = True) -> float:
        """Reset per-job state. Progress already made (power-loss resume,
        Moonraker restart) is treated as already notified. The job is only
        marked active once seeded, so status updates arriving meanwhile
        can't report old progress."""
        self._job_active = False
        self._job_done_sent = False
        if query:
            state = await self._query(
                {"print_stats": ["info"], "virtual_sdcard": ["file_position"],
                 **({"print_task_config": PREPRINT_FIELDS}
                    if self.printer.preprint_calibrations else {})})
            for obj in self._status:
                self._status[obj].update(state.get(obj) or {})
        self._gcode_bytes = self._get_gcode_bytes(filename)
        self._job_filename = filename
        layer = ((self._status["print_stats"].get("info") or {})
                 .get("current_layer") or 0)
        self._first_layer_sent = int(layer) > self.layers_complete
        self._layer_started_sent = int(layer) >= 1
        self._driver_hot_sent = False
        # Chamber: a job joined past its first layer has no wait to report
        self._ch = {"phase": "done" if int(layer) >= 1 else "none",
                    "target": 0., "source": "", "ref": None, "ref_time": 0.,
                    "stall_sent": False, "used": False, "faults": set(),
                    "fault": None, "connected": None,
                    "wait_start": 0., "announced": False, "gen": 0}
        self._by_object = await self.eventloop.run_in_thread(
            self._is_by_object, filename)
        self._job_slicer = await self.eventloop.run_in_thread(
            self._slicer_settings, filename)
        self._job_m73 = await self.eventloop.run_in_thread(
            self._scan_m73, filename)
        self._layer1_pd = None
        self._job_pauses = await self.eventloop.run_in_thread(
            self._scan_pauses, filename)
        self._pauses_seen = set()
        self._obj_start = {}
        self._obj_first_sent = set()
        self._obj_cur = self._status["exclude_object"].get("current_object")
        if self._obj_cur:
            # Joined part-way through this object (power loss, Moonraker
            # restart): its layers so far are unknown, so don't report them
            self._obj_start[self._obj_cur] = -10 ** 6
            self._obj_first_sent.add(self._obj_cur)
        if self._by_object:
            logging.info("[pushraker] %s is printed by object",
                         shown(filename))
        pct = self._progress() * 100.
        self._milestones_sent = {m for m in MILESTONES if m <= pct}
        self._report_next = None
        if self._first_layer_sent or self._milestones_sent:
            self._skip("progress", f"job already at layer {layer}, {pct:.0f}%")
        self._job_active = True
        return pct / 100.

    # ---- print hooks ---------------------------------------------------

    async def _on_remote(self, event: str = "", **_kw: Any) -> None:
        if event in ("complete", "cancelled"):
            self._job_done_sent = True
            state = await self._query({"print_stats": None})
            ps = state.get("print_stats") or {}
            ex = self._live_fault(ps)
            reported = ex is not None and self._fault_recent(fault_key(ex))
            if event == "cancelled" and ex is not None and not reported:
                # Mark now, so the job 'error' event that follows a
                # fault-cancel is not reported a second time
                self._mark_fault(fault_key(ex))
            self._spawn(self._report_end(event, ps, from_hook=True,
                                         fault_reported=reported))
        else:
            logging.warning("[pushraker] unknown hook event: %s",
                            event)

    async def _report_end(
        self, event: str, ps: Dict[str, Any], from_hook: bool = False,
        fault_reported: bool = False
    ) -> None:
        """`fault_reported`: the fault behind this cancel was already sent
        with its pause, so this is a plain cancel that names it."""
        self._job_done_sent = True
        filename = ps.get("filename") or ""
        ex = self._live_fault(ps)
        if event == "complete":
            rid, title = "complete", self._title("complete")
            body = self._lines(
                shown(filename),
                "Print time: "
                f"{format_duration(ps.get('print_duration', 0))}",
                self._queue_line(filename, starting=False))
        elif ex is not None and fault_reported:
            rid, title = "cancelled", self._title("cancelled")
            body = f"{shown(filename)}\nAfter: {describe(ex)}"
        elif ex is not None:
            rid = self._fault_row()
            if rid is None:
                return
            if self._fault_recent(fault_key(ex)) and rid == "error" \
                    and not from_hook:
                self._skip("error", f"{format_code(ex)} already reported")
                return
            self._mark_fault(fault_key(ex))
            title = self._title("error", kind="cancelled") \
                if rid == "error" else self._title("cancelled")
            body = f"{shown(filename)}\n{describe(ex)}"
        else:
            rid, title, body = "cancelled", self._title("cancelled"), shown(filename)
        row = self.rows.get(rid)
        if row is None:
            return
        # Grab the snapshot right away: the hook reports once the toolhead
        # has lifted, and the stock macro keeps raising it from there
        snapshot = await self._fetch_snapshot() if row[0] == "snapshot" \
            else None
        await self._send(rid, title, body, snapshot=snapshot,
                         snapshot_ready=True)

    # ---- job queue -----------------------------------------------------

    def _queue_line(self, filename: str, starting: bool) -> str:
        """Moonraker's job queue, if it has jobs: "5 prints remaining in
        queue (total 9h 12m)". At print start the count and the time
        include the print that just started; when a print ends they are
        what is still queued. Nothing when the queue is empty."""
        try:
            jq = self.server.lookup_component("job_queue", None)
        except Exception:
            jq = None
        if jq is None:
            return ""
        files = [str(j) for j in list(getattr(jq, "queued_jobs", {})
                                      .values())]
        state = getattr(jq, "queue_state", "")
        # The queue starts its next job before taking it off the list
        if starting and files and state in ("loading", "starting") and \
                files[0] == filename:
            files = files[1:]
        if not files:
            return ""
        if starting:
            files = [filename] + files
        total = sum(self._estimated_time(f) for f in files)
        n = len(files)
        line = (f"Print 1/{n} remaining in queue" if starting
                else f"{n} print{'s' if n != 1 else ''} remaining in queue")
        return line + (f" (total {format_duration(total)})" if total > 0
                       else "")

    # ---- exceptions ----------------------------------------------------

    # Event handlers must return quickly; do the waiting in a task.
    def _on_gcode_response(self, response: str) -> None:
        """Notifications from G-code and macros: a console line
        "notify <title> || <message> || <priority>" (RESPOND
        PREFIX=notify MSG=...). The title is everything before the first
        " || ", the priority the last field if it is one of min..max, the
        message everything in between. On printers without this component
        the line is only console output."""
        for line in str(response or "").splitlines():
            if not line.startswith(MACRO_PREFIX):
                continue
            parts = line[len(MACRO_PREFIX):].split(MACRO_SEP)
            priority = self.macro_priority
            if len(parts) > 1 and parts[-1].strip().lower() in MACRO_LEVELS:
                priority = parts.pop().strip().lower()
            title = parts[0].strip()
            message = MACRO_SEP.join(parts[1:]).strip()
            rid = f"macro_{priority}"
            if not title or rid not in self.rows:
                if title:
                    self._skip("macro", f"{priority} notifications are off")
                continue
            self._spawn(self._send(
                rid, self._title("macro", title=title), message,
                filename=self._job_filename if self._job_active else ""))

    def _track_calibration(self) -> None:
        """Manual calibrations (touchscreen: bed levelling, XYZ offset,
        flow, input shaper), one row: "<which> complete" when its machine
        state ends (idle, or straight into the next queued calibration),
        and "user intervention needed" when it reaches a step that waits
        for a person. The bed mesh inside a print job's start routine sets
        the same state, so nothing is reported while a print job is
        active."""
        if "calibration" not in self.rows:
            return
        msm = self._status["machine_state_manager"]
        try:
            state = int(msm.get("main_state"))
            action = int(msm.get("action_code") or 0)
        except (TypeError, ValueError):
            return
        if self._job_active:
            self._calibrating = self._cal_action = None
            return
        if state != self._calibrating:
            done, self._calibrating = self._calibrating, None
            self._cal_action = None
            self._cal_hold += 1
            cal = self.printer.calibrations
            if done is not None and state == self.printer.msm_abnormal:
                self._spawn(self._send(
                    "calibration",
                    self._title("calibration_failed",
                                what=cal[done]),
                    "The printer reports an abnormal state; check the "
                    "screen.", camera_wait=0.))
            elif done is not None and (state == self.printer.msm_idle
                                       or state in cal):
                self._spawn(self._send(
                    "calibration",
                    self._title("calibration", what=cal[done]),
                    "Printer is idle." if state == self.printer.msm_idle else
                    f"{cal[state]} starts next.", camera_wait=0.))
            if state in cal:
                self._calibrating = state
            else:
                return
        if action == self._cal_action:
            return
        self._cal_action = action
        self._cal_hold += 1
        what = self.printer.calibrations[state]
        if action in self.printer.manual_clean:
            self._spawn(self._send(
                "calibration", self._title("intervention"),
                self._lines(what, f"Brush the nozzle of Toolhead "
                            f"{self.printer.manual_clean[action]}, then "
                            "tap the screen."),
                camera_wait=0.))
        elif action == 0:
            self._spawn(self._intervention_hold(self._cal_hold, what))

    # ---- chamber temperature -------------------------------------------

    def _chamber_now(self) -> Tuple[float, Optional[float], str]:
        """(target, chamber temperature, source) right now: the first
        heater_generic with a target, else Snapmaker's chamber wait while it
        runs. No target: (0, None, "")."""
        for h in self._ch_heaters:
            st = self._status.get(h) or {}
            try:
                target = float(st.get("target") or 0.)
                temp = st.get("temperature")
                if target > 0:
                    return target, (None if temp is None else float(temp)), h
            except (TypeError, ValueError):
                continue
        pur = self._status.get("purifier") or {}
        msm = self._status["machine_state_manager"]
        try:
            target = float(pur.get("desired_chamber_temp") or 0.)
            action = int(msm.get("action_code") or 0)
        except (TypeError, ValueError):
            return 0., None, ""
        if pur.get("power_detected") and target > 0:
            temp = (self._status.get(self.printer.chamber_sensor)
                    or {}).get("temperature")
            waiting = action == self.printer.action_preheat_chamber
            return target, (None if temp is None else float(temp)), \
                "purifier" if waiting else "purifier_idle"
        return 0., None, ""

    def _track_chamber(self) -> None:
        """Before the first layer: waiting / reached / timed out / not
        heating. Through the whole job: DragonBreath faults and losing it."""
        ch = self._ch
        ps = self._status["print_stats"]
        filename = shown(ps.get("filename") or self._job_filename)
        layer = (ps.get("info") or {}).get("current_layer")
        try:
            layer = int(layer or 0)
        except (TypeError, ValueError):
            layer = 0
        target, temp, source = self._chamber_now()
        if source == DRAGONBREATH or source.endswith(" " + DRAGONBREATH):
            ch["used"] = True
        now = time.monotonic()
        deg = lambda t: f"{t:.0f} °C"
        state = lambda t, T: f"Chamber {deg(t)}, target {deg(T)}."
        if ch["phase"] in ("none", "waiting") and temp is not None:
            if layer >= 1:
                if ch["phase"] == "waiting" and ch["announced"]:
                    T = ch["target"]
                    if temp >= T - CHAMBER_TOLERANCE:
                        self._chamber_send("chamber_reached", "chamber_reached",
                                           filename, state(temp, T),
                                           "Printing…")
                    else:
                        self._chamber_send("chamber_fault", "chamber_timeout",
                                           filename, state(temp, T),
                                           "Printing below target chamber "
                                           "temperature…")
                ch["phase"] = "done"
            elif ch["phase"] == "none" and target > 0 and \
                    source != "purifier_idle" and (
                        temp < target if source == "purifier"
                        else temp < target - CHAMBER_TOLERANCE):
                ch.update(phase="waiting", target=target, source=source,
                          ref=temp, ref_time=now, wait_start=now,
                          announced=False, gen=ch["gen"] + 1)
                self._spawn(self._chamber_announce(ch["gen"], filename))
            elif ch["phase"] == "waiting":
                if target > 0 and source != "purifier_idle":
                    ch["target"] = target
                T = ch["target"]
                purifier_done = ch["source"] == "purifier" and \
                    source != "purifier"
                if temp >= T or purifier_done:
                    if not ch["announced"]:
                        pass    # over before it counted as a wait: silent
                    elif temp >= T:
                        self._chamber_send("chamber_reached",
                                           "chamber_reached", filename,
                                           state(temp, T), "Printing…")
                    else:
                        # Snapmaker's wait gave up (no rise for its TIMEOUT)
                        self._chamber_send("chamber_fault", "chamber_timeout",
                                           filename, state(temp, T),
                                           "Printing below target chamber "
                                           "temperature…")
                    ch["phase"] = "done"
                elif ch["ref"] is None or \
                        temp >= ch["ref"] + CHAMBER_STALL_RISE:
                    ch.update(ref=temp, ref_time=now)
                elif not ch["stall_sent"] and \
                        now - ch["ref_time"] >= CHAMBER_STALL_TIME:
                    ch["stall_sent"] = True
                    self._chamber_send(
                        "chamber_fault", "chamber_stalled", filename,
                        f"No rise in {CHAMBER_STALL_TIME / 60:.0f} min.",
                        f"Printer still waiting for {deg(T)} "
                        f"(currently {deg(temp)})…")
        if not self._ch_device:
            return
        dev = self._status.get(DRAGONBREATH) or {}
        if not ch["used"]:
            ch["connected"] = dev.get("connected")
            return

        def next_line() -> str:
            t = (self._status.get(f"heater_generic {DRAGONBREATH}") or {}) \
                .get("temperature")
            t = temp if t is None else float(t)
            if ch["phase"] == "waiting":
                return (f"Printer still waiting for {deg(ch['target'])}"
                        + (f" (currently {deg(t)})…" if t is not None
                           else "…"))
            return ("Print job continuing"
                    + (f", chamber {deg(t)}…" if t is not None else "…"))
        connected = dev.get("connected")
        if ch["connected"] and connected is False:
            self._chamber_send(
                "chamber_fault", "chamber_disconnected", filename,
                "DragonBreath heater not responding, should shut off when "
                "disconnected from printer.", next_line())
        ch["connected"] = connected
        reason = str(dev.get("fault_reason") or "fault").strip()
        if dev.get("fault"):
            if ch["fault"] != reason and reason not in ch["faults"]:
                ch["faults"].add(reason)
                self._chamber_send(
                    "chamber_fault", "chamber_fault", filename,
                    f"DragonBreath heater fault: {reason}.", next_line())
            ch["fault"] = reason
        else:
            ch["fault"] = None

    async def _chamber_announce(self, gen: int, filename: str) -> None:
        """"Waiting for chamber temperature", once the wait has lasted
        CHAMBER_WAIT_MIN (a wait that ended sooner sends nothing)."""
        await asyncio.sleep(CHAMBER_WAIT_MIN)
        ch = self._ch
        if not self._job_active or ch.get("gen") != gen or \
                ch["phase"] != "waiting":
            return
        ch["announced"] = True
        _target, temp, _source = self._chamber_now()
        if temp is None:
            temp = ch["ref"] or 0.
        self._chamber_send("chamber_waiting", "chamber_waiting", filename,
                           f"Chamber {temp:.0f} °C, target "
                           f"{ch['target']:.0f} °C.", "Printer waiting…")

    def _chamber_send(self, rid: str, title: str, filename: str,
                      *lines: str) -> None:
        if rid not in self.rows:
            return
        self._spawn(self._send(rid, self._title(title),
                               self._lines(filename, *lines)))

    # ---- filament loaded / unloaded ----------------------------------

    def _track_filament_change(self) -> None:
        ptc = self._status["print_task_config"]
        exist = [bool(x) for x in (ptc.get("filament_exist") or [])]
        if not exist:
            return
        for ch in range(len(exist)):
            name = self._printer_filament(ptc, ch)
            if name:
                self._exist_names[ch] = name
        if self._exist_seen is None or len(self._exist_seen) != len(exist):
            self._exist_seen = exist        # seed: nothing to report yet
            return
        for ch, (was, now) in enumerate(zip(self._exist_seen, exist)):
            if was != now:
                self._exist_gen += 1
                self._spawn(self._filament_change(ch, now, self._exist_gen))

    async def _filament_change(self, ch: int, now: bool, gen: int) -> None:
        await asyncio.sleep(FILAMENT_CHANGE_SETTLE)
        if gen != self._exist_gen:
            return      # changed again meanwhile: the newer wait reports
        ptc = self._status["print_task_config"]
        exist = [bool(x) for x in (ptc.get("filament_exist") or [])]
        if ch >= len(exist) or exist[ch] != now or self._exist_seen is None:
            return
        if self._exist_seen[ch] == now:
            return      # already reported (e.g. the same change twice)
        self._exist_seen[ch] = now
        if self._job_active or "filament_change" not in self.rows or \
                ("loaded" if now else "unloaded") \
                not in self.filament_change_events:
            return      # a print job's runouts and refills say it instead
        # Loaded: the printer's filament info arrives shortly after the
        # sensor; unloaded: what it had (the info may be cleared by now)
        ids = list(ptc.get("filament_spool_id") or [])
        sid = ids[ch] if ch < len(ids) else 0
        spool = await self._spool(sid) if now and self._spoolman_active() \
            else None
        remaining = self._remaining(spool)
        name = self._spool_name(spool) or self._printer_filament(ptc, ch) \
            or self._exist_names.get(ch, "")
        if name:
            self._exist_names[ch] = name
        title = self._title("filament_loaded" if now else "filament_unloaded")
        body = self._lines(
            f"{self._toolhead(ch)}: {name}" if name else self._toolhead(ch),
            f"{remaining:.0f} g left" if remaining is not None
            and remaining > 0 else "")
        # The spool picture: its colour from Spoolman or the printer, the
        # amount only when Spoolman knows it
        row = dict(toolhead=self._toolhead(ch), word="", status="ink",
                   needs="", remaining=remaining, full=self._full_weight(spool),
                   colour=self._spool_colour(spool, ch), name=name)
        await self._send_parts("filament_change", [(title, body, [row])])

    async def _intervention_hold(self, hold: int, what: str) -> None:
        start = time.monotonic()
        while True:
            now = time.monotonic()
            if self._status["idle_timeout"].get("state") == "Printing":
                self._cal_busy_at = now
            due = max(start, self._cal_busy_at) + INTERVENTION_HOLD
            if now >= due:
                break
            await asyncio.sleep(min(due - now, 1.))
            if hold != self._cal_hold or self._job_active:
                return      # the calibration moved on (or ended) meanwhile
        await self._send("calibration", self._title("intervention"),
                         self._lines(what, "The screen is waiting for you."),
                         camera_wait=0.)

    async def _on_exception(self, excep: Dict[str, Any]) -> None:
        if fault_key(excep) == self._stale_fault:
            self._stale_fault = None    # it happened again
        self._spawn(self._handle_exception(dict(excep)))

    async def _on_klippy_shutdown(self) -> None:
        self._spawn(self._handle_shutdown())

    async def _handle_exception(self, excep: Dict[str, Any]) -> None:
        key = fault_key(excep)
        level = int(excep.get("level") or 0)
        if level < self.min_level:
            return
        if key in self._faults_pending or self._fault_recent(key):
            self._skip("fault", f"{format_code(excep)} already reported")
            return
        self._faults_pending.add(key)
        try:
            # Let a pause/cancel caused by this fault report it first
            await asyncio.sleep(self.settle_time)
            if self._fault_recent(key):
                self._skip("fault", f"{format_code(excep)} reported with the pause or cancel")
                return
            state = await self._query({"print_stats": None, "webhooks": None})
            ps = state.get("print_stats") or {}
            shutdown = (state.get("webhooks") or {}).get("state") == "shutdown"
            if shutdown:
                rid, title = "shutdown", self._title("shutdown")
            elif level <= 1:
                rid, title = "warning", self._title("warning")
            else:
                rid, title = "error", self._title("error", kind="fault")
            if rid not in self.rows:
                return
            details = describe(excep)
            filename = ps.get("filename") or ""
            if filename and ps.get("state") in ("printing", "paused"):
                details = f"{shown(filename)}\n{details}"
            self._mark_fault(key)
            await self._send(rid, title, details.strip())
        finally:
            self._faults_pending.discard(key)

    async def _handle_shutdown(self) -> None:
        # Fallback for shutdowns that never produced a coded exception.
        if "shutdown" not in self.rows:
            return
        await asyncio.sleep(self.settle_time + 2.)
        if time.monotonic() - self._last_fault_send < self.settle_time + 10.:
            self._skip("shutdown", "already reported with its error code")
            return
        state = await self._query({"print_stats": None, "webhooks": None})
        wh = state.get("webhooks") or {}
        if wh.get("state") != "shutdown":
            return
        ps = state.get("print_stats") or {}
        msg = (wh.get("state_message") or "").strip()
        msg = msg.splitlines()[0] if msg else "Klipper shutdown"
        # print_stats keeps the last file name after the job: name it only
        # if the shutdown interrupted it
        filename = ps.get("filename") or "" \
            if ps.get("state") in ("printing", "paused") else ""
        self._last_fault_send = time.monotonic()
        await self._send("shutdown", self._title("shutdown"),
                         f"{shown(filename)}\n{msg}" if filename else msg)

    # ---- print progress ------------------------------------------------

    async def _on_klippy_ready(self) -> None:
        first_ready = not self._ready_once
        self._ready_once = True
        # Klipper restarted: its macro variables are back at their defaults
        self.hook_values = {}
        kapis: APIComp = self.server.lookup_component("klippy_apis")
        chamber = {}
        try:
            objs = await kapis.get_object_list(default=[])
        except Exception:
            objs = []
        self.printer = printers.detect(objs)
        if first_ready:
            logging.info("[pushraker] printer: %s (drivers: %s, "
                         "extruders: %d)", self.printer.name,
                         ", ".join(self.printer.drivers) or "none",
                         len(self.printer.extruders))
        if self.store is not None:
            # The printer, its webcams and Spoolman are known now
            self.store.unavailable = tuple(self.printer.rows_unavailable)
            try:
                if not self.store.exists():
                    # A settings file to edit by hand from the start: the
                    # older setup's settings when the harness names them,
                    # else this printer's defaults
                    self.store.save(self._first_settings())
                    logging.info("[pushraker] wrote %s",
                                 self.store.settings_path)
                await self._reload("klippy ready")
            except Exception as e:
                logging.error("[pushraker] settings: %s", e)
        for obj in list(self.printer.drivers) + self.printer.extruders:
            self._status.setdefault(obj, {})
        self._ace_on = ACE_OBJECT in objs
        # Subscribed whether or not their rows are on: the rows can be
        # turned on from the page at any time (the objects are cheap)
        if True:
            self._ch_heaters = [o for o in objs
                                if o.startswith("heater_generic ")]
            self._ch_sensor = bool(self.printer.chamber_sensor) and \
                self.printer.chamber_sensor in objs
            self._ch_purifier = "purifier" in objs
            self._ch_device = DRAGONBREATH in objs
            chamber = dict(
                {h: ["temperature", "target"] for h in self._ch_heaters},
                **({self.printer.chamber_sensor: ["temperature"]}
                   if self._ch_sensor else {}),
                **({"purifier": ["power_detected", "desired_chamber_temp"]}
                   if self._ch_purifier else {}),
                **({DRAGONBREATH: ["fault", "fault_reason", "connected"]}
                   if self._ch_device else {}))
            for obj in chamber:
                self._status.setdefault(obj, {})
        try:
            status = await kapis.subscribe_objects(
                dict({"print_stats": ["state", "filename", "info",
                                      "print_duration", "filament_used"],
                      "virtual_sdcard": ["progress", "file_position"],
                      "exclude_object": ["objects", "current_object",
                                         "excluded_objects"],
                      **{d: ["temperature"] for d in self.printer.drivers},
                      # Always: the runout hold and the backup-spool switch
                      # read the toolhead map and the refill settings from
                      # it, with or without Spoolman
                      **({"print_task_config": PTC_FIELDS}
                         if self.printer.toolhead_map else {}),
                      **{e: NOZZLE_FIELDS for e in self.printer.extruders}},
                     **({"machine_state_manager": ["main_state",
                                                   "action_code"]}
                        if "machine_state_manager" in objs else {}),
                     **{"idle_timeout": ["state"]},
                     **({ACE_OBJECT: ACE_FIELDS} if self._ace_on else {}),
                     **chamber),
                self._on_status_update, default={})
        except Exception as e:
            logging.error("[pushraker] subscribe failed: %s", e)
            status = {}
        for obj in self._status:
            self._status[obj].update(status.get(obj) or {})
        self._calibrating = self._cal_action = None   # Klipper restarted
        self._ace_src = self._ace_sources()
        self._exist_seen = None
        self._exist_gen += 1
        self._track_filament_change()
        self._cal_hold += 1
        ps = self._status["print_stats"]
        if ps.get("state") in ("printing", "paused"):
            # Started before Moonraker: track it without re-notifying
            await self._start_job(ps.get("filename") or "", query=False)
            self._start_filament_watch(ps.get("filename") or "",
                                       self._progress(), check_start=False)
        else:
            self._job_active = False
        if first_ready and "power_loss" in self.rows and \
                self.printer.power_loss_env_paths and self._booted_recently():
            self._spawn(self._check_power_loss())

    async def _on_status_update(
        self, data: Dict[str, Any], _eventtime: float
    ) -> None:
        for obj, values in data.items():
            if obj in self._status and isinstance(values, dict):
                self._status[obj].update(values)
        ps = self._status["print_stats"]
        if "print_task_config" in data and self._job_active:
            self._check_spool_switch()
        idle = (data.get("idle_timeout") or {}).get("state")
        if idle == "Printing" or (idle == "Ready" and self._idle_was == "Printing"):
            # Busy, or busy until just now: the intervention hold counts
            # from the moment Klipper went idle
            self._cal_busy_at = time.monotonic()
        if idle:
            self._idle_was = idle
        if "machine_state_manager" in data:
            self._track_calibration()
            self._track_preprint()
        if "filament_exist" in (data.get("print_task_config") or {}):
            self._track_filament_change()
        if ACE_OBJECT in data:
            self._track_ace()
        if self._job_active and self._ch:
            self._track_chamber()
        if not self._job_active or ps.get("state") != "printing":
            return
        filename = ps.get("filename") or ""
        if "driver_hot" in self.rows and not self._driver_hot_sent and \
                any(d in data for d in self.printer.drivers):
            temps = {axis: (self._status.get(d) or {}).get("temperature")
                     for d, axis in self.printer.drivers.items()}
            hot = [a for a, t in temps.items()
                   if t is not None and float(t) >= self.driver_hot]
            if hot:
                self._driver_hot_sent = True
                self._spawn(self._send(
                    "driver_hot", self._title("driver_hot"),
                    self._lines(shown(filename), *(
                        f"{a} driver: {temps[a]:.0f} °C"
                        + (f" (alert at {self.driver_hot:.0f} °C)"
                           if a in hot else "")
                        for a in self.printer.drivers.values()
                        if temps[a] is not None))))
        if self._by_object and not self._objects():
            # No EXCLUDE_OBJECT markers (label objects off): nothing to
            # track per object, so it's reported like a by-layer job
            self._by_object = False
            logging.info("[pushraker] no object markers in %s: "
                         "reported as one job", shown(filename))
        if not self._pp_sent:
            # Layer 1 began: the start routine is over, even if a chosen
            # calibration was skipped (a toolhead already calibrated)
            layer = (ps.get("info") or {}).get("current_layer")
            if layer is not None and int(layer) >= 1:
                self._preprint_done(filename)
        if self._layer1_pd is None:
            layer = (ps.get("info") or {}).get("current_layer")
            if layer is not None and int(layer) >= 1:
                self._layer1_pd = float(ps.get("print_duration") or 0.)
        if "layer_started" in self.rows and not self._layer_started_sent:
            # Layer 1 begins once the start routine (homing, bed mesh,
            # flow calibration) is over: time to watch the first layer
            layer = (ps.get("info") or {}).get("current_layer")
            if layer is not None and int(layer) >= 1:
                self._layer_started_sent = True
                self._spawn(self._send(
                    "layer_started", self._title("layer_started"),
                    self._lines(shown(filename), self._time_left())))
        if self._by_object:
            self._track_objects(filename)
        elif "first_layer" in self.rows and not self._first_layer_sent:
            layer = (ps.get("info") or {}).get("current_layer")
            if layer is not None and int(layer) > self.layers_complete:
                self._first_layer_sent = True
                self._spawn(self._send(
                    "first_layer", self._title(
                        "first_layer",
                        ordinal=ORDINALS[self.layers_complete]),
                    self._lines(shown(filename), self._time_left())))
        pct = self._progress() * 100.
        crossed = [m for m in MILESTONES
                   if m <= pct and m not in self._milestones_sent]
        if crossed:
            # Only report the highest one if several were crossed at once
            self._milestones_sent.update(crossed)
            for m in sorted(crossed, reverse=True):
                if f"progress_{m}" in self.rows:
                    self._spawn(self._send(
                        f"progress_{m}", self._title("progress", pct=m),
                        self._lines(shown(filename), self._time_left())))
                    break
        if "status_report" in self.rows:
            self._status_report(filename, pct)

    def _status_report(self, filename: str, pct: float) -> None:
        """Every report_interval minutes of print time (print_duration,
        which Klipper stops while paused), the first one an interval after
        the job started; none at the very end."""
        pd = float(self._status["print_stats"].get("print_duration") or 0.)
        if self._report_next is None:
            self._report_next = pd + self.report_interval * 60.
            return
        if pd < self._report_next or pct >= 100.:
            return
        self._report_next = pd + self.report_interval * 60.
        info = self._status["print_stats"].get("info") or {}
        layer, total = info.get("current_layer"), info.get("total_layer")
        parts = [f"{pct:.0f}% complete"]
        if self._by_object and self._obj_cur:
            parts.append(str(self._obj_cur))
        if layer:
            parts.append(f"layer {int(layer)}"
                         + (f" of {int(total)}" if total else ""))
        self._spawn(self._send(
            "status_report", self._title("status_report"),
            self._lines(shown(filename), ", ".join(parts) + ".",
                        self._time_left())))

    def _is_by_object(self, filename: str) -> bool:
        """Whether the slicer's config block (at the end of the file) says
        the print job is by object. Runs in a thread: it reads the file."""
        if not filename:
            return False
        try:
            fm = self.server.lookup_component("file_manager")
            path = os.path.join(fm.get_directory("gcodes"), filename)
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - CONFIG_TAIL))
                return RX_BY_OBJECT.search(f.read()) is not None
        except Exception as e:
            logging.warning("[pushraker] could not read %s: %s",
                            shown(filename), e)
            return False

    def _scan_m73(self, filename: str) -> List[Tuple[int, float]]:
        """The slicer's own time-left markers, M73 R<minutes>, with their
        byte offsets (PrusaSlicer, Orca and its forks write one per layer
        or percent). Runs in a thread: it reads the whole file."""
        if not filename:
            return []
        try:
            fm = self.server.lookup_component("file_manager")
            path = os.path.join(fm.get_directory("gcodes"), filename)
            out: List[Tuple[int, float]] = []
            with open(path, "rb") as f:
                data = f.read()
            for m in RX_M73.finditer(data):
                out.append((m.start(), float(m.group(1))))
            return out
        except Exception as e:
            logging.warning("[pushraker] could not read %s: %s",
                            shown(filename), e)
            return []

    def _slicer_settings(self, filename: str) -> Dict[str, str]:
        """The slicer's "; key = value" settings at the end of the file.
        Runs in a thread: it reads the file."""
        if not filename:
            return {}
        try:
            fm = self.server.lookup_component("file_manager")
            path = os.path.join(fm.get_directory("gcodes"), filename)
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - CONFIG_TAIL))
                return tp.slicer_settings(f.read())
        except Exception as e:
            logging.warning("[pushraker] could not read %s: %s",
                            shown(filename), e)
            return {}

    def _scan_pauses(self, filename: str) -> List[Dict[str, Any]]:
        """The pauses the print job's file asks for: [{offset, layer,
        message, kind}], in file order. Runs in a thread: it reads the
        file."""
        if not filename:
            return []
        out: List[Dict[str, Any]] = []
        try:
            fm = self.server.lookup_component("file_manager")
            path = os.path.join(fm.get_directory("gcodes"), filename)
            layer = 0
            m117 = (b"", -10 ** 9)   # last M117 text and where
            with open(path, "rb") as f:
                pos = 0
                for line in f:
                    at, pos = pos, pos + len(line)
                    if not line.startswith((b";", b"P", b"p", b"M", b"m")):
                        continue
                    m = RX_LAYER.match(line)
                    if m:
                        layer = int(m.group(2)) + 1 if m.group(2) else layer + 1
                        continue
                    m = RX_M117.match(line)
                    if m:
                        m117 = (m.group(1).strip(), at)
                        continue
                    if RX_PAUSE_MARK.match(line):
                        if out and at - out[-1]["offset"] <= PAUSE_MERGE:
                            continue
                        out.append(dict(offset=at, layer=layer, message="",
                                        kind=""))
                        continue
                    m = RX_PAUSE_CMD.match(line)
                    if not m:
                        continue
                    kind = ("filament change"
                            if m.group(1).upper() == b"M600" else "")
                    msg = (m.group(3) or b"").strip()
                    if not msg and at - m117[1] <= PAUSE_MERGE:
                        msg = m117[0]
                    text = msg.decode("utf-8", "replace")
                    if out and at - out[-1]["offset"] <= PAUSE_MERGE \
                            and not out[-1]["kind"]:
                        out[-1].update(message=out[-1]["message"] or text,
                                       kind=kind)
                        continue
                    out.append(dict(offset=at, layer=layer, kind=kind,
                                    message=text))
        except Exception as e:
            logging.warning("[pushraker] could not read %s: %s",
                            shown(filename), e)
        if out:
            logging.info("[pushraker] %s has %d pause(s) in the "
                         "file: %s", shown(filename), len(out),
                         ", ".join(f"layer {p['layer']} @{p['offset']}"
                                   for p in out))
        return out

    # ---- pre-print calibrations ----------------------------------------

    def _preprint_seed(self, recovered: bool) -> None:
        """At the job's start: which pre-print calibrations were chosen
        (print_task_config), and so which runs to wait for."""
        self._pp_names, self._pp_pending = [], set()
        self._pp_running, self._pp_sent = None, True
        cal = self.printer.preprint_calibrations
        if not cal or recovered:
            return
        ptc = self._status["print_task_config"]
        used = list(ptc.get("extruders_used") or [])
        which = list(ptc.get("flow_calib_extruders") or [])
        for flag, name in cal.items():
            if not ptc.get(flag):
                continue
            self._pp_names.append(name)
            per_tool = sorted({i for c, (f, i) in
                               self.printer.preprint_codes.items()
                               if f == flag and i is not None})
            if not per_tool:
                self._pp_pending.add((flag, None))
                continue
            for i in per_tool:
                if (i >= len(which) or which[i]) and \
                        (i >= len(used) or used[i]):
                    self._pp_pending.add((flag, i))
        self._pp_sent = not self._pp_names

    def _preprint_line(self) -> str:
        if not self._pp_names:
            return ""
        # A blank line before it unless the pauses line has one
        return ("" if self._job_pauses else "\n") + \
            "📐 Pre-print calibrations selected: " + \
            ", ".join(self._pp_names) + "."

    def _track_preprint(self) -> None:
        """During the job's start routine: each chosen calibration's
        action_code while it runs, back to idle when done; when none is
        left, they are complete (also when layer 1 begins first)."""
        if self._pp_sent or not self._job_active:
            return
        try:
            action = int(self._status["machine_state_manager"]
                         .get("action_code") or 0)
        except (TypeError, ValueError):
            return
        step = self.printer.preprint_codes.get(action)
        if step is not None:
            self._pp_running = step
            return
        if self._pp_running is None or action != 0:
            return
        self._pp_pending.discard(self._pp_running)
        self._pp_running = None
        if not self._pp_pending:
            self._preprint_done(self._job_filename)

    def _preprint_done(self, filename: str) -> None:
        self._pp_sent = True
        if cfgmod.PREPRINT_ROW not in self.rows:
            return
        names = self._pp_names
        done = names[0] if len(names) == 1 else \
            ", ".join(names[:-1]) + " and " + names[-1]
        self._spawn(self._send(
            cfgmod.PREPRINT_ROW, self._title("preprint_done"),
            self._lines(shown(filename),
                        f"{done} done. Starting the print job.",
                        self._time_left()),
            filename=filename))

    def _pauses_line(self) -> str:
        """For the started notification: how many pauses the file has."""
        ps = self._job_pauses
        if not ps:
            return ""
        layers = [p["layer"] for p in ps if p["layer"]]
        where = ""
        if len(layers) == len(ps):
            where = (f" (layer {layers[0]})" if len(layers) == 1 else
                     " (layers " + ", ".join(str(n) for n in layers[:-1])
                     + f" and {layers[-1]})")
        return ("\n⏸️ Print job contains a pause" if len(ps) == 1 else
                f"\n⏸️ Print job contains {len(ps)} pauses") + where + "."

    def _file_pause(self, position: Any) -> Optional[int]:
        """Index of the file's pause the print job stopped at, if the file
        position is just past one of them and it hasn't been reported."""
        try:
            pos = int(position)
        except (TypeError, ValueError):
            return None
        for i, p in enumerate(self._job_pauses):
            if i not in self._pauses_seen and \
                    p["offset"] <= pos <= p["offset"] + PAUSE_WINDOW:
                return i
        return None

    async def _file_pause_alert(self, filename: str) -> None:
        state = await self._query({"virtual_sdcard": ["file_position"]})
        pos = (state.get("virtual_sdcard") or {}).get("file_position")
        i = self._file_pause(pos)
        if i is None:
            await self._send("paused", self._title("paused"), shown(filename),
                             delay=PAUSE_SNAPSHOT_DELAY)
            return
        self._pauses_seen.add(i)
        p = self._job_pauses[i]
        n = len(self._job_pauses)
        what = (f"Pause {i + 1} of {n}" if n > 1 else "Pause") + (
            f" at layer {p['layer']}" if p["layer"] else "") + (
            " for a filament change" if p["kind"] else "")
        line = f"{what}, as planned in the print job."
        if p["message"]:
            line += f' "{p["message"]}"'
        # The Paused row's destinations at the highest priority (the
        # file_pause row the config writes next to paused)
        await self._send("file_pause" if "file_pause" in self.rows
                         else "paused", self._title("file_pause"),
                         self._lines(shown(filename), line,
                                     "Waiting for you to resume…"),
                         delay=PAUSE_SNAPSHOT_DELAY)

    def _objects(self) -> List[str]:
        return [str(o.get("name") or "") for o in
                self._status["exclude_object"].get("objects") or []]

    def _object_line(self, name: str) -> str:
        """'Object 2 of 3: Assembly' for a notification body."""
        objects = self._objects()
        n = objects.index(name) + 1 if name in objects else 0
        # Orca names objects <model>_id_<n>_copy_<m>; show the model, and
        # a number when the same model is on the plate more than once
        pretty = name
        m = RX_OBJECT_NAME.match(name)
        if m:
            base = m.group(1)
            same = [o for o in objects
                    if (RX_OBJECT_NAME.match(o) or m).group(1) == base]
            pretty = f"{base} #{same.index(name) + 1}" if len(same) > 1 \
                else base
        where = f"Object {n} of {len(objects)}" if n else "Object"
        return f"{where}: {pretty}"

    def _track_objects(self, filename: str) -> None:
        """Print-by-object: report an object's first layers and its
        completion. Layer numbers run on across the objects (Orca), so an
        object's layer is counted from the layer it began on; a new object
        beginning means the previous one is complete."""
        ex = self._status["exclude_object"]
        cur = ex.get("current_object") or None
        layer = int((self._status["print_stats"].get("info") or {})
                    .get("current_layer") or 0)
        if cur and cur != self._obj_cur:
            prev = self._obj_cur
            self._obj_cur = cur
            self._obj_start.setdefault(cur, layer)
            if prev is not None and prev != cur and \
                    prev not in (ex.get("excluded_objects") or []):
                self._object_done(prev, filename)
        cur = self._obj_cur
        if not cur or "first_layer" not in self.rows or \
                cur in self._obj_first_sent or \
                cur in (ex.get("excluded_objects") or []):
            return
        if self.first_layer_objects != "each" and self._first_layer_sent:
            return
        if layer - self._obj_start.get(cur, layer) >= self.layers_complete:
            self._obj_first_sent.add(cur)
            self._first_layer_sent = True
            self._spawn(self._send(
                "first_layer",
                self._title("first_layer",
                            ordinal=ORDINALS[self.layers_complete]),
                self._lines(shown(filename), self._object_line(cur),
                            self._time_left())))

    def _object_done(self, name: str, filename: str) -> None:
        if "object_complete" not in self.rows:
            return
        self._spawn(self._send(
            "object_complete", self._title("object_complete"),
            self._lines(shown(filename), self._object_line(name),
                        self._time_left())))

    def _get_gcode_bytes(self, filename: str) -> Tuple[Any, Any]:
        if not filename:
            return (None, None)
        try:
            fm = self.server.lookup_component("file_manager")
            meta = fm.get_metadata_storage().get(filename, {}) or {}
            return (meta.get("gcode_start_byte"), meta.get("gcode_end_byte"))
        except Exception as e:
            logging.warning("[pushraker] metadata lookup failed: %s",
                            e)
            return (None, None)

    def _progress(self) -> float:
        # Same calculation as Fluidd's default ("file relative"), which is
        # also what the Snapmaker app shows.
        sd = self._status["virtual_sdcard"]
        start, end = self._gcode_bytes
        pos = sd.get("file_position")
        if start is not None and end is not None and pos is not None:
            if pos <= start:
                return 0.
            if pos >= end:
                return 1.
            if end > start:
                return (pos - start) / (end - start)
        return float(sd.get("progress") or 0.)

    # ---- power loss ----------------------------------------------------

    @staticmethod
    def _booted_recently() -> bool:
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0]) < BOOT_WINDOW
        except Exception:
            return False

    async def _check_power_loss(self) -> None:
        # pl_env_valid is set once the MCUs have reported their saved
        # state, which can take a while after klippy_ready.
        deadline = time.monotonic() + PL_WAIT_TIME
        while time.monotonic() < deadline:
            result = await self._query({"virtual_sdcard": ["pl_env_valid"]})
            if (result.get("virtual_sdcard") or {}).get("pl_env_valid"):
                break
            await asyncio.sleep(2.)
        else:
            return
        filename = ""
        env_path = next((p for p in self.printer.power_loss_env_paths
                         if os.path.isfile(p)), None)
        if env_path is not None:
            try:
                with open(env_path, "r") as f:
                    filename = os.path.basename(
                        json.load(f).get("file_path") or "")
            except Exception as e:
                logging.warning("[pushraker] power-loss file "
                                "unreadable: %s", e)
        self._pl_file = filename
        details = "Print interrupted by power loss. " \
            "Resume is available on the touchscreen."
        await self._send("power_loss", self._title("power_loss"),
                         f"{shown(filename)}\n{details}" if filename else details,
                         camera_wait=PL_CAMERA_WAIT)

    # ---- Spoolman: filament checks ----------------------------------------
    #
    # Toolheads are numbered 1-4 for people; channels are 0-3. The slicer's
    # filaments (T0..) are mapped to channels by print_task_config's
    # extruder_map_table, and each channel's Spoolman spool is in
    # filament_spool_id. Only toolheads the print uses are checked.
    #
    #  * filament_short: a toolhead has not enough filament for the rest of
    #    the print (red), or only just enough (orange, within FILAMENT_CLOSE).
    #    Checked at print start (one notification for all toolheads at risk)
    #    and every FILAMENT_POLL during the print (only when a toolhead gets
    #    worse than already reported). Nothing is sent when all is fine.
    #  * filament_low: a spool is down to filament_low_percent of a full
    #    spool or filament_low_grams, whichever comes first. Once per spool
    #    (remembered across restarts) until it is refilled or replaced.
    #  * spool_switch: the printer refilled from another toolhead.

    def _spoolman_active(self) -> bool:
        return bool(self.spoolman_url) and any(
            r in self.rows for r in SPOOLMAN_ROWS)

    def _start_filament_watch(
        self, filename: str, progress: float, check_start: bool
    ) -> None:
        self._job_map = list(
            self._status["print_task_config"].get("extruder_map_table") or [])
        self._job_ptc = dict(self._status["print_task_config"])
        self._job_tools = set()
        self._job_weights = {}
        self._switched_to = set()
        self._slot_empty_sent = set()
        self._job_progress0 = progress
        self._spool_start = {}
        self._spool_now = {}
        self._tool_spools = {}
        self._filament_job += 1
        self._spawn(self._filament_watch(
            self._filament_job, filename, progress, check_start))

    async def _spool(self, spool_id: int) -> Optional[Dict[str, Any]]:
        if not spool_id:
            return None
        try:
            http = self.server.lookup_component("http_client")
            resp = await http.get(
                f"{self.spoolman_url}/api/v1/spool/{spool_id}",
                enable_cache=False)
            if resp.status_code == 200:
                return resp.json()
            logging.warning("[pushraker] Spoolman spool %d: HTTP "
                            "%s", spool_id, resp.status_code)
        except Exception as e:
            logging.warning("[pushraker] Spoolman spool %d: %s",
                            spool_id, e)
        return None

    @staticmethod
    def _remaining(spool: Optional[Dict[str, Any]]) -> Optional[float]:
        r = (spool or {}).get("remaining_weight")
        return float(r) if isinstance(r, (int, float)) else None

    @staticmethod
    def _full_weight(spool: Optional[Dict[str, Any]]) -> Optional[float]:
        """A full spool's filament weight: the spool's own initial weight,
        else the filament's net weight."""
        sp = spool or {}
        for w in (sp.get("initial_weight"),
                  (sp.get("filament") or {}).get("weight")):
            if isinstance(w, (int, float)) and w > 0:
                return float(w)
        return None

    @staticmethod
    def _toolhead(channel: int) -> str:
        return f"Toolhead {channel + 1}"

    @staticmethod
    def _spool_name(spool: Optional[Dict[str, Any]]) -> str:
        fil = (spool or {}).get("filament") or {}
        vendor = (fil.get("vendor") or {}).get("name") or ""
        fname = fil.get("name") or fil.get("material") or ""
        # Spoolman filament names often already start with the vendor
        if vendor and fname.lower().startswith(vendor.lower()):
            vendor = ""
        return " ".join(x for x in (vendor, fname) if x)

    def _spool_colour(self, spool: Optional[Dict[str, Any]],
                      ch: int) -> Any:
        """The filament's colour for the picture: one RGB, or a list of
        them for a multi-colour filament (Spoolman's multi_color_hexes,
        else the printer's filament_color_multi), falling back to the
        single colour (Spoolman's color_hex, the printer's rgba)."""
        if images is None:
            return None
        fil = (spool or {}).get("filament") or {}
        multi = [images.parse_hex(v) for v in
                 str(fil.get("multi_color_hexes") or "").split(",") if v]
        multi = [c for c in multi if c]
        if len(multi) > 1:
            return multi
        single = images.parse_hex(fil.get("color_hex"))
        if single:
            return single
        if multi:
            return multi[0]
        ptc = self._job_ptc or self._status["print_task_config"]
        pm = list(ptc.get("filament_color_multi") or [])
        if ch < len(pm) and isinstance(pm[ch], dict):
            cols = [images.parse_hex(v) for v in (pm[ch].get("colors") or [])]
            cols = [c for c in cols if c]
            if len(cols) > 1:
                return cols
            if cols:
                return cols[0]
        cols = list(ptc.get("filament_color_rgba") or [])
        if ch < len(cols):
            return images.parse_hex(cols[ch])
        return None

    @staticmethod
    def _cap(text: str, cap: Optional[int]) -> str:
        if cap is None or len(text) <= cap:
            return text
        return text[:max(1, cap - 1)].rstrip() + "…"

    @staticmethod
    def _printer_filament(ptc: Dict[str, Any], ch: int) -> str:
        """The filament as the printer knows it (no Spoolman): vendor and
        type, e.g. "Elegoo PLA"."""
        parts = []
        for k in ("filament_vendor", "filament_type"):
            v = list(ptc.get(k) or [])
            val = str(v[ch]).strip() if ch < len(v) else ""
            if val and val.upper() != "NONE":
                parts.append(val)
        return " ".join(parts)

    def _desc(self, ch: int, name: str, sid: int,
              cap: Optional[int] = None) -> str:
        inner = ", ".join(x for x in (self._cap(name, cap),
                                      f"spool #{sid}" if sid else "") if x)
        return f"{self._toolhead(ch)} ({inner})" if inner \
            else self._toolhead(ch)

    def _needed_grams(self, filename: str) -> Dict[int, float]:
        """Slicer filament weight per slicer filament (T0..)."""
        try:
            fm = self.server.lookup_component("file_manager")
            meta = fm.get_metadata_storage().get(filename, {}) or {}
        except Exception:
            return {}
        weights = meta.get("filament_weight")
        if isinstance(weights, (int, float)):
            weights = [weights]
        if not isinstance(weights, list):
            total = meta.get("filament_weight_total")
            weights = [total] if total else []
        return {i: float(w) for i, w in enumerate(weights)
                if isinstance(w, (int, float)) and w > 0}

    def _nozzle(self, ch: int) -> Tuple[Any, Any]:
        ex = self.printer.extruders
        st = self._status.get(ex[ch] if ch < len(ex) else "") or {}
        return st.get("nozzle_diameter"), st.get("nozzle_volume_type")

    @staticmethod
    def _colour_multi(src: Dict[str, Any], c: int) -> Any:
        """The printer compares colours on filament_color_multi (the whole
        set, for multi-colour filaments); older firmware has only
        filament_color_rgba."""
        multi = list(src.get("filament_color_multi") or [])
        if c < len(multi) and isinstance(multi[c], dict) and multi[c]:
            return multi[c]
        rgba = list(src.get("filament_color_rgba") or [])
        return {"colors": [str(rgba[c])[:6].upper()]} if c < len(rgba) \
            else None

    def _refill_check(self, ptc: Dict[str, Any], ch: int, c: int,
                      src: Dict[str, Any]) -> Tuple[bool, str]:
        """Whether the printer may refill `ch` from `c`, as
        print_task_config.py (cmd_INNER_AUTO_REPLENISH_FILAMENT) decides
        it, and if not, why, in words for the notification ("" when `c`
        isn't even the same material, which isn't worth a line)."""
        def field(d: Dict[str, Any], k: str, i: int) -> str:
            v = list(d.get(k) or [])
            return str(v[i]).strip() if i < len(v) else ""
        exist = list(ptc.get("filament_exist") or [])
        if c < len(exist) and not exist[c]:
            return False, ""
        same_type = field(ptc, "filament_type", c).lower() == \
            field(src, "filament_type", ch).lower() != ""
        nd, nv = self._nozzle(ch)
        cd, cv = self._nozzle(c)
        if nd is not None and cd is not None and (nd != cd or nv != cv):
            return False, (f"nozzle {cd} vs {nd} mm" if nd != cd
                           else f"{cv or 'other'} nozzle vs {nv or 'other'}"
                           ) if same_type else ""
        if field(ptc, "filament_vendor", c).upper() in ("", "NONE"):
            return False, ("no filament info set on the printer (vendor "
                           "NONE)" if same_type else "")
        for k, what in (("filament_vendor", "vendor"),
                        ("filament_type", "type"),
                        ("filament_sub_type", "sub-type")):
            if field(ptc, k, c).lower() != field(src, k, ch).lower():
                return False, (f"different {what} ({field(ptc, k, c) or 'none'}"
                               f" vs {field(src, k, ch) or 'none'})"
                               if same_type else "")
        if not ptc.get("replenish_ignore_color") and \
                self._colour_multi(ptc, c) != self._colour_multi(src, ch):
            return False, "colour differs (refill needs an exact match)"
        return True, ""

    def _backups(self, ptc: Dict[str, Any], ch: int,
                 attrs_from: Optional[Dict[str, Any]] = None) -> List[int]:
        """Channels the printer can refill `ch` from, in the order it would
        use them (each one refilled from in turn as the one before runs
        out). `attrs_from` gives `ch`'s own filament (e.g. as it was at
        print start, before it ran out).

        print_task_config.py (cmd_INNER_AUTO_REPLENISH_FILAMENT): same
        nozzle, a vendor set (not NONE), vendor, type and sub-type equal.
        Of those, the first exact colour match (filament_color_multi) in
        toolhead order; else, with "ignore colour", the nearest first
        colour (Rec. 601 weighted RGB distance). When that spool runs out,
        the next is chosen the same way from it."""
        if not ptc.get("auto_replenish_filament"):
            return []
        ignore = bool(ptc.get("replenish_ignore_color"))

        def first_colour(src: Dict[str, Any], c: int) -> str:
            m = self._colour_multi(src, c) or {}
            cols = list(m.get("colors") or [])
            return str(cols[0]).upper()[:6] if cols else ""

        def distance(a: str, b: str) -> float:
            try:
                d = [int(a[i:i + 2], 16) - int(b[i:i + 2], 16)
                     for i in (0, 2, 4)]
            except ValueError:
                return float("inf")
            return ((0.299 * d[0]) ** 2 + (0.587 * d[1]) ** 2
                    + (0.114 * d[2]) ** 2) ** 0.5
        ids = list(ptc.get("filament_spool_id") or [])
        src = attrs_from or ptc
        relaxed = dict(ptc, replenish_ignore_color=True)
        left = [c for c in range(len(ids)) if c != ch
                and self._refill_check(relaxed, ch, c, src)[0]]
        order: List[int] = []
        cur_src, cur = src, ch
        while left:
            exact = [c for c in left if self._colour_multi(ptc, c)
                     == self._colour_multi(cur_src, cur)]
            if exact:
                pick = exact[0]
            elif ignore:
                tc = first_colour(cur_src, cur)
                pick = min(left, key=lambda c: (
                    distance(first_colour(ptc, c), tc), c))
            else:
                break
            order.append(pick)
            left.remove(pick)
            cur_src, cur = ptc, pick
        return order

    def _refill_blockers(self, ptc: Dict[str, Any], ch: int,
                         attrs_from: Optional[Dict[str, Any]] = None
                         ) -> List[Tuple[int, str]]:
        """Toolheads with the same material as `ch` that the printer will
        not refill from, and why: the moment to fix it is now."""
        src = attrs_from or ptc
        ids = list(ptc.get("filament_spool_id") or [])
        out = []
        for c in range(len(ids)):
            if c == ch:
                continue
            ok, why = self._refill_check(ptc, ch, c, src)
            if not ok and why:
                out.append((c, why))
        return out

    @staticmethod
    def _status_of(remaining: float, need: float) -> str:
        """red: not enough; orange: enough, but only just; green: fine."""
        if remaining < need:
            return "red"
        if remaining < need + max(FILAMENT_CLOSE_G, need * FILAMENT_CLOSE):
            return "orange"
        return "green"

    def _seen(self, sid: int, remaining: Optional[float]) -> None:
        """Spoolman's weight for a spool, as read during the job."""
        if not sid or remaining is None or remaining <= 0:
            return
        self._spool_start.setdefault(sid, remaining)
        self._spool_now[sid] = remaining

    def _used(self) -> Optional[Dict[int, float]]:
        """Grams each spool lost since the job started, or None if Spoolman
        isn't tracking usage (nothing used yet well into the print)."""
        used = {sid: max(0., g - self._spool_now.get(sid, g))
                for sid, g in self._spool_start.items()}
        if sum(used.values()) < USAGE_MIN_G and \
                self._progress() > USAGE_GRACE:
            return None
        return used

    def _need_now(self, ch: int) -> Optional[float]:
        """Grams the rest of the print job still needs from channel `ch`:
        the slicer's total for the slicer filaments on it now, minus what
        their spools (including refill spools they moved from) used since
        the job started, per Spoolman. None when that can't be measured:
        no slicer weights, Spoolman not tracking usage, or the watch began
        partway through the job (e.g. power-loss recovery)."""
        if not self._job_weights or \
                self._job_progress0 > RECOVERY_MIN_PROGRESS:
            return None
        used = self._used()
        if used is None:
            return None
        emap = self._job_map or []
        tools = [t for t in self._job_weights
                 if (emap[t] if t < len(emap) else t) == ch]
        if not tools:
            return None
        total = sum(self._job_weights[t] for t in tools)
        spools: Set[int] = set()
        for t in tools:
            spools |= self._tool_spools.get(t, set())
        return max(0., total - sum(used.get(sid, 0.) for sid in spools))

    # -- low-spool memory (once per spool until it is refilled) --

    def _low_load(self) -> Dict[str, float]:
        if self._low_sent is None:
            if (not os.path.exists(self._low_path)
                    and os.path.exists(self._old_low_path)):
                try:
                    os.replace(self._old_low_path, self._low_path)
                except OSError:
                    pass
            try:
                with open(self._low_path) as f:
                    data = json.load(f)
                self._low_sent = {str(k): float(v) for k, v in
                                  (data.get("low_sent") or {}).items()}
                th = data.get("thread")
                if self._thread is None and isinstance(th, dict) and \
                        th.get("id") and "file" in th:
                    self._thread = {"id": str(th["id"]),
                                    "file": str(th["file"])}
            except Exception:
                self._low_sent = {}
        return self._low_sent

    def _low_save(self) -> None:
        try:
            tmp = self._low_path + ".tmp"
            state = {"low_sent": self._low_sent or {}}
            if self._thread:
                state["thread"] = self._thread
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self._low_path)
        except Exception as e:
            logging.warning("[pushraker] can't save %s: %s",
                            self._low_path, e)

    def _low_threshold(self, full: Optional[float]) -> float:
        """Whichever comes first: % of a full spool, or grams."""
        pct = (self.filament_low_percent / 100. * full
               if self.filament_low_percent and full else 0.)
        return max(pct, self.filament_low_grams or 0.)

    def _low_mark(self, sid: int) -> None:
        self._low_load()[str(sid)] = time.time()
        self._low_save()

    async def _check_low(
        self, ptc: Dict[str, Any], ch: int, sid: int,
        spool: Optional[Dict[str, Any]], remaining: float,
        needs: Dict[int, float]
    ) -> Optional[Dict[str, Any]]:
        """The spool if it just went below the running-low threshold (sent
        together with the others from the same check, see _low_alert)."""
        if remaining <= 0:
            return None         # amount unknown
        sent = self._low_load()
        thr = self._low_threshold(self._full_weight(spool))
        key = str(sid)
        if key in sent and (not thr or remaining > thr):
            # Refilled, re-weighed or replaced: may alert again later
            del sent[key]
            self._low_save()
            return None
        if not thr or remaining > thr or key in sent:
            return None
        self._low_mark(sid)
        if "filament_low" not in self.rows:
            return None
        # With refill on, the spools the printer would refill it from
        return await self._issue(ptc, ch, sid, spool, remaining,
                                 needs.get(ch) or 0., needs)

    @staticmethod
    def _enough(it: Dict[str, Any]) -> bool:
        return it["rem"] >= it["need"]

    def _low_need_line(self, it: Dict[str, Any]) -> str:
        """Running low isn't running out: say whether this print fits."""
        return (f"This print needs ~{it['need']:.0f} g: "
                + ("enough is left for it." if self._enough(it)
                   else "more than is left."))

    def _low_text(self, it: Dict[str, Any], cap: Optional[int]) -> str:
        left = (f"{it['rem']:.0f} of {it['full']:.0f} g left" if it["full"]
                else f"{it['rem']:.0f} g left")
        lines = [f"{self._desc(it['ch'], it['name'], it['sid'], cap)}: "
                 f"{left}.",
                 self._low_need_line(it) if it["need"] else ""]
        if it["refill_on"] and it["backups"]:
            for b in it["backups"]:
                lines.append(
                    f"Refill is on: "
                    f"{self._desc(b['ch'], b['name'], b['sid'], cap)}"
                    + (f", {b['rem']:.0f} g left" if b["rem"] > 0
                       else ", amount left unknown")
                    + f", is {self._match_text(b)}.")
        elif it["refill_on"]:
            lines.append("Refill is on. " + (
                "No exact match spool for refill found on printer."
                if it["exact"] else
                "No compatible spool for refill found on printer."))
        lines.extend(self._blocker_lines(it))
        return self._lines(*lines)

    @staticmethod
    def _match_text(b: Dict[str, Any]) -> str:
        return ("an exact match" if b.get("exact") else
                "an inexact match (same filament, different colour)")

    def _blocker_lines(self, it: Dict[str, Any]) -> List[str]:
        return [f"Won't refill from {self._toolhead(c)}: {why}."
                for c, why in it.get("blockers") or []]

    def _low_row(self, it: Dict[str, Any]) -> Dict[str, Any]:
        """Like the not-enough picture (with the refill spool, or a note
        that there is none), worded as running low."""
        row = self._issue_row(dict(it, status="orange"))
        row.update(word="Running low.",
                   needs=((f"Needs ~{it['need']:.0f} g, "
                           + ("enough left." if self._enough(it)
                              else "more than left."))
                          if it["need"] else ""))
        for r in row.get("refills") or []:
            r["status"] = "ink" if not it["need"] else r["status"]
        return row

    async def _low_alert(self, items: List[Optional[Dict[str, Any]]]) -> None:
        """Spools that went below the threshold in the same check: one
        notification, in toolhead order (split only if the service's
        length limit requires it)."""
        lows = sorted((it for it in items if it), key=lambda i: i["ch"])
        if not lows:
            return
        title = self._title("filament_low") if len(lows) == 1 else \
            self._title("filament_lows", n=len(lows))
        await self._send_parts("filament_low", None, packer=(
            title, "", lows, self._low_text, self._low_row))

    # -- not enough filament --

    async def _issue(
        self, ptc: Dict[str, Any], ch: int, sid: int,
        spool: Optional[Dict[str, Any]], remaining: float, need: float,
        needs: Dict[int, float]
    ) -> Dict[str, Any]:
        """Everything a not-enough notification says about one toolhead,
        including the spools the printer could refill it from."""
        ids = list(ptc.get("filament_spool_id") or [])
        it: Dict[str, Any] = dict(
            ch=ch, sid=sid, name=self._spool_name(spool), rem=remaining,
            full=self._full_weight(spool), colour=self._spool_colour(spool, ch),
            need=need, status=self._status_of(remaining, need),
            refill_on=bool(ptc.get("auto_replenish_filament")),
            exact=not ptc.get("replenish_ignore_color"), backups=[])
        unknown_backup = False
        total = max(0., remaining)

        own_colour = self._colour_multi(self._job_ptc or ptc, ch)
        it["blockers"] = self._refill_blockers(ptc, ch, self._job_ptc or None) \
            if it["refill_on"] else []
        for c in self._backups(ptc, ch, self._job_ptc or None):
            b_sid = ids[c] if c < len(ids) else 0
            b = await self._spool(b_sid)
            b_rem = self._remaining(b)
            if b_rem is None:
                continue
            own = needs.get(c, 0.)
            if b_rem <= 0:
                # Loaded, but Spoolman thinks it's empty: amount unknown
                unknown_backup = True
            elif b_rem - own <= 0:
                # The print needs all of it itself: nothing to spare
                continue
            else:
                total += b_rem - own
            it["backups"].append(dict(
                ch=c, sid=b_sid, name=self._spool_name(b), rem=b_rem,
                full=self._full_weight(b), colour=self._spool_colour(b, c),
                own=own, exact=self._colour_multi(ptc, c) == own_colour))
        # multiACE: the slots Quad Replenish would reload from, before the
        # other toolheads (quad_first, the default) or after them. Their
        # amounts are not known here.
        ace_backups = self._ace_backups(ch)
        if ace_backups:
            unknown_backup = True
            it["backups"] = (ace_backups + it["backups"]
                             if self._ace().get("quad_first", True)
                             else it["backups"] + ace_backups)
        it["total"] = total
        it["together"] = self._status_of(total, need)
        it["unknown_backup"] = unknown_backup
        if unknown_backup and it["together"] != "green":
            it["together"] = "orange"
        if remaining <= 0:
            it["status"] = "unknown"
        return it

    def _issue_text(self, it: Dict[str, Any], cap: Optional[int]) -> str:
        desc = self._desc(it['ch'], it['name'], it['sid'], cap)
        if it["status"] == "unknown":
            lines = [f"{desc}: amount of filament left is unknown (Spoolman "
                     f"shows {it['rem']:.0f} g). Needs ~{it['need']:.0f} g."]
        else:
            what = ("not enough filament." if it["status"] == "red"
                    else "remaining filament may be insufficient.")
            lines = [f"{desc}: {what} Needs ~{it['need']:.0f} g, "
                     f"{it['rem']:.0f} g left."]
        if it["backups"]:
            for b in it["backups"]:
                if b.get("ace"):
                    lines.append(f"Quad Replenish is on: "
                                 f"{self._bdesc(b, cap)}, is an exact match.")
                    continue
                lines.append(
                    f"Refill is on: {self._bdesc(b, cap)}"
                    + (f", {b['rem']:.0f} g left" if b["rem"] > 0
                       else ", amount left unknown")
                    + f", is {self._match_text(b)}." + (
                        f" This print also needs ~{b['own']:.0f} g from it."
                        if b["own"] and b["rem"] > 0 else ""))
            unknown = it["unknown_backup"] or it["status"] == "unknown"
            if unknown and it["together"] == "green":
                lines.append(f"Together at least {it['total']:.0f} g: "
                             "enough for this print.")
            elif unknown:
                lines.append(f"Together {it['total']:.0f} g plus an unknown "
                             "amount: may be insufficient.")
            else:
                lines.append(f"Together {it['total']:.0f} g: " + {
                    "green": "enough for this print.",
                    "orange": "may be insufficient.",
                    "red": "still not enough."}[it["together"]])
        elif it["refill_on"]:
            lines.append("Refill is on. " + (
                "No exact match spool for refill found on printer."
                if it["exact"] else
                "No compatible spool for refill found on printer."))
        if not it["backups"]:
            lines.extend(self._ace_none_lines(it["ch"]))
        lines.extend(self._blocker_lines(it))
        return "\n".join(lines)

    def _issue_row(self, it: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(toolhead=self._toolhead(it["ch"]),
                   word={"red": "Not enough.", "orange": "May be insufficient.",
                         "unknown": "Amount unknown."}[it["status"]],
                   status=it["status"], needs=f"Needs ~{it['need']:.0f} g.",
                   remaining=it["rem"], full=it["full"], colour=it["colour"],
                   name=it["name"], unknown=it["status"] == "unknown")
        if it["backups"]:
            # The whole chain, in the order the printer would use it
            row["refills"] = [dict(
                toolhead=self._blabel(b), remaining=b["rem"],
                full=b["full"], colour=b["colour"], name=b["name"],
                status=it["together"],
                unknown=b["rem"] is not None and b["rem"] <= 0,
                match="Exact match" if b.get("exact") else "Inexact match")
                for b in it["backups"]]
        elif it["refill_on"]:
            blockers = it.get("blockers") or []
            if blockers:
                c, why = blockers[0]
                row["note"] = ["Refill is on.", f"Won't refill from "
                               f"{self._toolhead(c)}:", why]
            else:
                row["note"] = ["Refill is on.",
                               "No exact match spool for" if it["exact"]
                               else "No compatible spool for",
                               "refill found on printer."]
        elif self._ace_head(it["ch"]) is not None:
            row["note"] = self._ace_none_lines(it["ch"])
        return row

    def _short_title(self, issues: List[Dict[str, Any]]) -> str:
        if len(issues) == 1:
            return self._title({"red": "filament_short",
                                "orange": "filament_close",
                                "unknown": "filament_unknown"}[
                                    issues[0]["status"]])
        return self._title("filament_problems", n=len(issues))

    async def _short_alert(
        self, filename: str, issues: List[Dict[str, Any]]
    ) -> None:
        """One notification (or as few as the service's length limit
        allows) for all toolheads at risk, in toolhead order."""
        issues = sorted(issues, key=lambda i: i["ch"])
        title = self._short_title(issues)
        await self._send_parts("filament_short", None, packer=(
            title, shown(filename), issues, self._issue_text,
            self._issue_row))

    async def _filament_watch(
        self, job: int, filename: str, progress: float, check_start: bool
    ) -> None:
        ptc = (await self._query({"print_task_config": PTC_FIELDS})
               ).get("print_task_config") or {}
        self._status["print_task_config"].update(ptc)
        self._job_ptc = dict(ptc)
        ids = list(ptc.get("filament_spool_id") or [])
        emap = list(ptc.get("extruder_map_table") or [])
        self._job_map = emap
        weights = self._needed_grams(filename)
        self._job_weights = weights
        self._job_tools = set(weights) or {
            t for t, c in enumerate(emap[:len(ids)]) if isinstance(c, int)}
        if not self._spoolman_active():
            return      # seeded for the runout hold and the backup switch
        need: Dict[int, float] = {}
        for tool, grams in weights.items():
            ch = emap[tool] if tool < len(emap) else tool
            if isinstance(ch, int) and 0 <= ch < len(ids):
                # Started partway (power-loss recovery): what's left of the
                # job can't be measured, so only the spools are watched
                need[ch] = need.get(ch, 0.) + (
                    grams if progress <= RECOVERY_MIN_PROGRESS else 0.)
                if ids[ch]:
                    self._tool_spools.setdefault(tool, set()).add(ids[ch])
        if not need:
            used = list(ptc.get("extruders_used") or [])
            need = {ch: 0. for ch, u in enumerate(used) if u}
        # channel -> {sid, reported: 0 nothing, 1 orange, 2 red}
        track: Dict[int, Dict[str, Any]] = {}
        issues = []
        spools: Dict[int, Tuple[Optional[Dict[str, Any]], float]] = {}
        for ch in sorted(need):
            sid = ids[ch] if ch < len(ids) else 0
            if not sid:
                self._skip(f"filament ({self._toolhead(ch)})",
                           "no Spoolman spool assigned")
                continue
            spool = await self._spool(sid)
            remaining = self._remaining(spool)
            if remaining is None:
                self._skip(f"filament (spool #{sid})",
                           "Spoolman has no remaining weight for it")
                continue
            track[ch] = dict(sid=sid, reported=0)
            spools[ch] = (spool, remaining)
            self._seen(sid, remaining)
            # Spoolman at 0 g (or less) for a loaded spool: amount unknown
            if remaining <= 0 and not self.filament_unknown:
                continue
            if need[ch] > 0 and (remaining <= 0 or self._status_of(
                    remaining, need[ch]) != "green"):
                it = await self._issue(ptc, ch, sid, spool, remaining,
                                       need[ch], need)
                # Unknown amounts are only reported at print start
                track[ch]["reported"] = RANK.get(it["status"], 0)
                issues.append(it)
        if issues and check_start and "filament_short" in self.rows:
            # One warning at print start: a spool that is not enough (or
            # only just) is not also reported as running low
            for it in issues:
                self._low_mark(it["sid"])
            await self._short_alert(filename, issues)
        await self._low_alert([
            await self._check_low(ptc, ch, track[ch]["sid"], spool,
                                  remaining, need)
            for ch, (spool, remaining) in spools.items()])
        if not track:
            return
        logging.info("[pushraker] filament: watching %s", ", ".join(
            f"{self._toolhead(ch)} spool #{t['sid']}"
            for ch, t in track.items()))
        while True:
            await asyncio.sleep(FILAMENT_POLL)
            if job != self._filament_job or not self._job_active:
                return
            if self._status["print_stats"].get("state") != "printing":
                continue
            await self._poll_spools(filename, track)

    async def _poll_spools(
        self, filename: str, track: Dict[int, Dict[str, Any]]
    ) -> None:
        ptc = self._status["print_task_config"]
        ids = list(ptc.get("filament_spool_id") or [])
        # Toolheads the printer switched to (refill) are watched too
        for ch in self._switched_to - set(track):
            if ch < len(ids) and ids[ch]:
                track[ch] = dict(sid=ids[ch], reported=0)
        fetched: Dict[int, Tuple[int, Optional[Dict[str, Any]],
                                 Optional[float]]] = {}
        for ch, t in track.items():
            sid = ids[ch] if ch < len(ids) and ids[ch] else t["sid"]
            spool = await self._spool(sid)
            remaining = self._remaining(spool)
            self._seen(sid, remaining)
            fetched[ch] = (sid, spool, remaining)
        needs = {ch: self._need_now(ch) or 0. for ch in track}
        issues = []
        lows = []
        for ch, t in track.items():
            sid, spool, remaining = fetched[ch]
            if remaining is None:
                continue
            if sid != t["sid"]:
                logging.info("[pushraker] filament: %s now spool "
                             "#%d (%.0f g)", self._toolhead(ch), sid,
                             remaining)
                t.update(sid=sid, reported=0)
            need = needs.get(ch) or 0.
            if remaining <= 0:
                continue        # amount unknown: nothing to compare
            status = self._status_of(remaining, need) if need > 0 \
                else "green"
            # Only when it got worse than already reported; then it isn't
            # also reported as running low
            if RANK[status] > t["reported"] and "filament_short" in self.rows:
                t["reported"] = RANK[status]
                issues.append(await self._issue(
                    ptc, ch, sid, spool, remaining, need, needs))
                self._low_mark(sid)
            else:
                lows.append(await self._check_low(ptc, ch, sid, spool,
                                                  remaining, needs))
        if issues:
            await self._short_alert(filename, issues)
        await self._low_alert(lows)

    def _refill_expected(self, ex: Dict[str, Any]) -> bool:
        """A filament runout on a toolhead with a backup spool the printer
        will refill from by itself. Only when the refill would be reported
        (the Switched to backup spool row): a held-and-dropped error with
        nothing in its place would make the refill invisible."""
        if "spool_switch" not in self.rows:
            return False
        try:
            # 523 is the whole toolhead module (tangles, pickup and park
            # failures too); code 0 with "runout" is the runout itself
            runout = self.printer.runout_exception_id is not None and \
                int(ex.get("id", -1)) == self.printer.runout_exception_id and \
                int(ex.get("code", -1)) == 0 and \
                "runout" in str(ex.get("message", "")).lower()
            ch = int(ex.get("index", -1))
        except (TypeError, ValueError):
            return False
        ptc = self._status["print_task_config"]
        return runout and ch >= 0 and bool(
            self._backups(ptc, ch, self._job_ptc or None)
            or self._ace_candidates(ch))

    async def _runout_hold(self, filename: str, ex: Dict[str, Any]) -> None:
        refills = self._refills
        ch = int(ex.get("index", -1))
        ace = self._ace_head(ch) is not None
        if ace:
            self._ace_runouts.add(ch)
        deadline = time.monotonic() + (ACE_REFILL_WAIT if ace else REFILL_WAIT)
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if self._refills != refills:
                    self._skip("error", "runout refilled from a backup "
                               "spool (reported as Switched to backup spool)")
                    return
                if self._status["print_stats"].get("state") != "paused":
                    break   # resumed or ended without a refill being seen
        finally:
            self._ace_runouts.discard(ch)
        await self._send("error", self._title("error", kind="paused"),
                         f"{shown(filename)}\n{describe(ex)}")

    def _check_spool_switch(self) -> None:
        """A slicer filament moved to another toolhead mid-print: the
        printer refilled from a backup spool."""
        ptc = self._status["print_task_config"]
        new = list(ptc.get("extruder_map_table") or [])
        old = self._job_map or []
        if not new or new == old:
            return
        self._job_map = new
        if not old:
            return
        logging.info("[pushraker] filament: toolhead map changed "
                     "%s -> %s", old[:4], new[:4])
        # The printer refills while the print job is paused (it pauses on
        # the runout, swaps the map, then resumes), and resets the map when
        # the job ends: only a change during the job counts, and only one
        # that looks like a refill: every moved slicer filament leaves the
        # same (empty) toolhead, for a toolhead the printer is allowed to
        # refill it from. The reset comes ~10 s after the print-end hook,
        # while print_stats still says "printing", so the hook report
        # counts as the end too.
        if (self._job_done_sent or self._status["print_stats"].get("state")
                not in ("printing", "paused")):
            return
        moved = [t for t in range(min(len(old), len(new)))
                 if old[t] != new[t] and t in self._job_tools]
        if not moved or len(set(old[t] for t in moved)) != 1:
            return
        for tool in moved:
            if new[tool] not in self._backups(ptc, old[tool],
                                              self._job_ptc or None):
                logging.info("[pushraker] filament: toolhead %d -> "
                             "%d is not a refill, ignored",
                             old[tool] + 1, new[tool] + 1)
                continue
            self._switched_to.add(new[tool])
            self._refills += 1
            ids = list(ptc.get("filament_spool_id") or [])
            if new[tool] < len(ids) and ids[new[tool]]:
                self._tool_spools.setdefault(tool, set()).add(ids[new[tool]])
            if "spool_switch" in self.rows:
                self._spawn(self._switch_alert(tool, old[tool], new[tool]))

    async def _switch_alert(self, tool: int, frm: int, to: int) -> None:
        ptc = self._status["print_task_config"]
        ids = list(ptc.get("filament_spool_id") or [])
        f_sid = ids[frm] if frm < len(ids) else 0
        t_sid = ids[to] if to < len(ids) else 0
        if not f_sid:
            # The emptied toolhead's spool is unassigned on runout (SpoolLink
            # clears it before the refill): name the spool it had at start
            start = list((self._job_ptc or {}).get("filament_spool_id") or [])
            f_sid = start[frm] if frm < len(start) else 0
        f_spool, t_spool = await self._spool(f_sid), await self._spool(t_sid)
        f_rem = self._remaining(f_spool) or 0.
        t_rem = self._remaining(t_spool)
        need = self._need_now(to)
        filename = self._status["print_stats"].get("filename") or ""
        f_name = self._spool_name(f_spool) or \
            self._printer_filament(self._job_ptc or ptc, frm)
        t_name = self._spool_name(t_spool) or self._printer_filament(ptc, to)
        body = self._lines(
            shown(filename),
            f"From {self._desc(frm, f_name, f_sid)}.",
            f"To {self._desc(to, t_name, t_sid)}"
            + (f", {t_rem:.0f} g left" if t_rem is not None else "") + ".")
        row = dict(toolhead=self._toolhead(frm), word="Switched to refill.",
                   status="red", needs="", remaining=f_rem,
                   full=self._full_weight(f_spool),
                   colour=self._spool_colour(f_spool, frm), name=f_name,
                   refills=[dict(
                       toolhead=self._toolhead(to), remaining=t_rem or 0.,
                       full=self._full_weight(t_spool),
                       colour=self._spool_colour(t_spool, to), name=t_name,
                       status=(self._status_of(t_rem, need)
                               if need and t_rem is not None else "ink"))])
        await self._send_parts("spool_switch", [(
            self._title("spool_switch"), body, [row])])

    # -- multiACE --
    #
    # klipper/extras/ace.py exposes an `ace` object: head_source (the ACE
    # and slot feeding each head), aces[].slots[] (status, material,
    # brand, colour), mode, head_ace, quad_replenish, quad_first. Quad
    # Replenish (ace.py _quad_replenish_candidate) reloads a head that ran
    # out from the first slot whose gate has filament and whose declared
    # material equals the head's (colour too, when both declare one):
    # head mode, the other slots of the head's own ACE in slot order;
    # otherwise the same slot of the other ACEs. Nothing in the status
    # says "refilled": head_source changing while the runout is held is
    # the signal. Indices are 0-based, shown 1-based as multiACE does.

    def _ace(self) -> Dict[str, Any]:
        return self._status.get(ACE_OBJECT) or {}

    def _ace_sources(self) -> Dict[str, Tuple[int, int]]:
        """head_source as {head: (ace, slot)} for the heads an ACE feeds."""
        out: Dict[str, Tuple[int, int]] = {}
        for h, src in (self._ace().get("head_source") or {}).items():
            if isinstance(src, dict) and src.get("ace_index") is not None \
                    and src.get("slot") is not None:
                out[str(h)] = (int(src["ace_index"]), int(src["slot"]))
        return out

    def _ace_head(self, ch: int) -> Optional[Tuple[int, int]]:
        return self._ace_sources().get(str(ch))

    def _ace_slot(self, a: int, s: int) -> Dict[str, Any]:
        for i, unit in enumerate(self._ace().get("aces") or []):
            if int(unit.get("idx", i)) == a:
                slots = list(unit.get("slots") or [])
                return slots[s] if 0 <= s < len(slots) else {}
        return {}

    def _ace_ident(self, a: int, s: int) -> Optional[Tuple[str, str]]:
        """A slot's declared identity, (material, colour): material in
        lower case, colour as 6 hex digits or "" when not declared; None
        when the slot declares no material."""
        slot = self._ace_slot(a, s)
        mat = str(slot.get("material") or "").strip().lower()
        if not mat:
            return None
        col = slot.get("color")
        if isinstance(col, (list, tuple)) and len(col) == 3:
            try:
                hexcol = "%02X%02X%02X" % tuple(int(c) for c in col)
            except (TypeError, ValueError):
                hexcol = ""
        else:
            hexcol = str(col or "").strip().lstrip("#").upper()[:6]
        return mat, hexcol

    def _ace_candidates(self, ch: int) -> List[Tuple[int, int]]:
        """The slots Quad Replenish would reload head `ch` from, in the
        order it tries them (ace.py _quad_replenish_candidate)."""
        ace = self._ace()
        if not ace.get("quad_replenish"):
            return []
        src = (ace.get("head_source") or {}).get(str(ch))
        if not isinstance(src, dict) or src.get("ace_index") is None \
                or src.get("slot") is None:
            return []
        sa, ss = int(src["ace_index"]), int(src["slot"])
        want_t = str(src.get("type") or "").strip().lower()
        want_c = str(src.get("color") or "").strip().lstrip("#").upper()[:6]
        if not want_t:
            ident = self._ace_ident(sa, ss)
            if ident is None:
                return []
            want_t, want_c = ident
        if ace.get("mode") == "head":
            try:
                wired = int((ace.get("head_ace") or {}).get(str(ch), ch))
            except (TypeError, ValueError):
                wired = ch
            lanes = [(wired, s) for s in range(4)
                     if not (wired == sa and s == ss)]
        else:
            idxs = sorted(int(u.get("idx", i)) for i, u in
                          enumerate(ace.get("aces") or []))
            lanes = [(a, ch) for a in idxs if a != sa]
        out = []
        for a, s in lanes:
            if self._ace_slot(a, s).get("status") != "ready":
                continue    # gate empty, or busy
            ident = self._ace_ident(a, s)
            if ident is None:
                continue
            mat, col = ident
            if mat != want_t or (want_c and col and col != want_c):
                continue
            out.append((a, s))
        return out

    @staticmethod
    def _ace_label(a: int, s: int) -> str:
        return f"ACE {a + 1} slot {s + 1}"

    def _ace_name(self, a: int, s: int, ch: int) -> str:
        """Brand and material of a slot ("Generic PLA"), falling back to
        what head `ch` captured when it loaded from it."""
        slot = self._ace_slot(a, s)
        parts = [str(slot.get(k) or "").strip() for k in ("brand", "material")]
        name = " ".join(x for x in parts if x)
        if name:
            return name
        src = (self._ace().get("head_source") or {}).get(str(ch)) or {}
        parts = [str(src.get(k) or "").strip() for k in ("brand", "type")]
        return " ".join(x for x in parts if x)

    def _ace_colour(self, a: int, s: int, ch: int) -> Any:
        if images is None:
            return None
        ident = self._ace_ident(a, s)
        if ident and ident[1]:
            return images.parse_hex(ident[1])
        src = (self._ace().get("head_source") or {}).get(str(ch)) or {}
        return images.parse_hex(src.get("color"))

    def _ace_backups(self, ch: int) -> List[Dict[str, Any]]:
        """Quad Replenish's slots for head `ch` as backup entries (like
        _issue's toolhead backups; amounts unknown)."""
        return [dict(ch=ch, ace=(a, s), sid=0, name=self._ace_name(a, s, ch),
                     rem=None, full=None, colour=self._ace_colour(a, s, ch),
                     own=0., exact=True)
                for a, s in self._ace_candidates(ch)]

    def _ace_none_lines(self, ch: int) -> List[str]:
        """Why Quad Replenish has nothing for head `ch` (an ACE-fed head
        only)."""
        src = self._ace_head(ch)
        if src is None:
            return []
        if not self._ace().get("quad_replenish"):
            return ["Quad Replenish is off."]
        where = f"ACE {src[0] + 1}" if self._ace().get("mode") == "head" \
            else "the other ACEs"
        return ["Quad Replenish is on.",
                f"No matching slot on {where}."]

    def _blabel(self, b: Dict[str, Any]) -> str:
        return self._ace_label(*b["ace"]) if b.get("ace") \
            else self._toolhead(b["ch"])

    def _bdesc(self, b: Dict[str, Any], cap: Optional[int] = None) -> str:
        if b.get("ace"):
            name = self._cap(b["name"], cap)
            return f"{self._ace_label(*b['ace'])} ({name})" if name \
                else self._ace_label(*b["ace"])
        return self._desc(b["ch"], b["name"], b["sid"], cap)

    def _job_channels(self) -> Set[int]:
        emap = self._job_map or []
        chs = {emap[t] for t in self._job_tools
               if t < len(emap) and isinstance(emap[t], int)}
        return chs or set(range(len(self.printer.extruders)))

    def _track_ace(self) -> None:
        """head_source changing for a head whose runout is being held: a
        Quad Replenish reload (the print job resumes by itself). A slot
        feeding a toolhead of the job going empty: the toolhead is on the
        last of that spool (the filament path between the ACE and the
        nozzle), reported once per slot."""
        new = self._ace_sources()
        old, self._ace_src = self._ace_src, new
        state = self._status["print_stats"].get("state")
        if not self._job_active or state not in ("printing", "paused"):
            return
        for h in list(self._ace_runouts):
            o, n = old.get(str(h)), new.get(str(h))
            if n is not None and n != o:
                self._ace_runouts.discard(h)
                self._refills += 1
                logging.info("[pushraker] filament: Toolhead %d "
                             "reloaded from %s", h + 1, self._ace_label(*n))
                if "spool_switch" in self.rows:
                    self._spawn(self._ace_switch_alert(h, o, n))
        if "slot_empty" not in self.rows or state != "printing":
            return
        for h, (a, s) in new.items():
            ch = int(h)
            if ch not in self._job_channels() or (a, s) in self._slot_empty_sent:
                continue
            if self._ace_slot(a, s).get("status") == "empty":
                self._slot_empty_sent.add((a, s))
                self._spawn(self._slot_empty_alert(ch, a, s))

    def _ace_next_lines(self, ch: int, runs_out: str) -> List[str]:
        nxt = self._ace_candidates(ch)
        if nxt:
            return [f"Next: {self._bdesc(b)}, an exact match."
                    for b in self._ace_backups(ch)]
        none = self._ace_none_lines(ch)
        if none:
            none[-1] = none[-1].rstrip(".") + f": {runs_out}"
        return none

    def _ace_row(self, ch: int, a: int, s: int, word: str,
                 remaining: Optional[float], status: str,
                 refills: List[Dict[str, Any]]) -> Dict[str, Any]:
        row = dict(toolhead=self._ace_label(a, s), word=word, status=status,
                   needs="", remaining=remaining, full=None,
                   colour=self._ace_colour(a, s, ch),
                   name=self._ace_name(a, s, ch))
        if refills:
            row["refills"] = [dict(
                toolhead=self._blabel(b), remaining=None, full=None,
                colour=b["colour"], name=b["name"], status="ink",
                match="Exact match") for b in refills]
        elif self._ace_none_lines(ch):
            row["note"] = self._ace_none_lines(ch)
        return row

    async def _slot_empty_alert(self, ch: int, a: int, s: int) -> None:
        filename = self._status["print_stats"].get("filename") or ""
        th = self._toolhead(ch)
        body = self._lines(
            shown(filename),
            f"{self._ace_label(a, s)} ({self._ace_name(a, s, ch)}), feeding "
            f"{th}, has run out. {th} is printing what is left in the "
            "filament path.",
            *self._ace_next_lines(
                ch, f"the print job will pause when {th} runs out."))
        row = self._ace_row(ch, a, s, "Ran empty.", 0., "red",
                            self._ace_backups(ch))
        await self._send_parts("slot_empty", [(
            self._title("slot_empty"), body, [row])])

    async def _ace_switch_alert(self, ch: int, old: Optional[Tuple[int, int]],
                                new: Tuple[int, int]) -> None:
        filename = self._status["print_stats"].get("filename") or ""
        frm = (f"{self._ace_label(*old)} ({self._ace_name(*old, ch)})"
               if old else "an empty slot")
        body = self._lines(
            shown(filename),
            f"From {frm} on {self._toolhead(ch)}.",
            f"To {self._ace_label(*new)} ({self._ace_name(*new, ch)}).",
            *self._ace_next_lines(
                ch, f"the print job will pause when it runs out."))
        row = self._ace_row(ch, *(old or new), "Switched to refill.", 0.,
                            "red", [dict(ace=new, ch=ch,
                                         name=self._ace_name(*new, ch),
                                         colour=self._ace_colour(*new, ch))]
                            + self._ace_backups(ch))
        await self._send_parts("spool_switch", [(
            self._title("spool_switch"), body, [row])])

    # -- sending, split to fit the service --

    @staticmethod
    def _fits(channel: str, text: str) -> bool:
        limit = BODY_LIMITS.get(channel)
        if limit is None:
            return True
        chars, n = limit
        return (len(text) if chars else len(text.encode())) <= n

    def _pack(
        self, channel: str, header: str, issues: List[Dict[str, Any]],
        text_fn: Any
    ) -> List[Tuple[List[int], str]]:
        """Split the issues into as few messages as fit the channel. One
        toolhead's text is never split: if it doesn't fit on its own, the
        file name and then the spool names are shortened (all names to the
        same length, just enough)."""
        def body(head: str, blocks: List[str]) -> str:
            return "\n\n".join([head + "\n" + blocks[0]] + blocks[1:]) \
                if head else "\n\n".join(blocks)

        def fitted(i: int) -> Tuple[str, str]:
            for head in (header, self._cap(header, FILE_CAP)):
                block = text_fn(issues[i], None)
                if self._fits(channel, body(head, [block])):
                    return head, block
            head = self._cap(header, FILE_CAP)
            longest = max([len(issues[i]["name"])] +
                          [len(b["name"]) for b in issues[i]["backups"]])
            lo, hi = NAME_CAP_MIN, max(NAME_CAP_MIN, longest)
            while lo < hi:      # longest cap that fits
                mid = (lo + hi + 1) // 2
                if self._fits(channel, body(head, [text_fn(issues[i], mid)])):
                    lo = mid
                else:
                    hi = mid - 1
            return head, text_fn(issues[i], lo)

        out: List[Tuple[List[int], str]] = []
        idx: List[int] = []
        blocks: List[str] = []
        head_cur = header
        for i in range(len(issues)):
            head, block = fitted(i)
            if idx:
                for h in (head_cur, self._cap(header, FILE_CAP)):
                    if self._fits(channel, body(h, blocks + [block])):
                        head_cur = h
                        idx.append(i)
                        blocks.append(block)
                        break
                else:
                    out.append((idx, body(head_cur, blocks)))
                    idx, blocks, head_cur = [i], [block], head
            else:
                idx, blocks, head_cur = [i], [block], head
        if idx:
            out.append((idx, body(head_cur, blocks)))
        return out

    def _render_rows(self, rows: List[Dict[str, Any]]) -> Optional[str]:
        if images is None:
            return None
        tmpdir = tempfile.mkdtemp(prefix="pushraker_")
        try:
            return images.render(rows, os.path.join(tmpdir, "spools.jpg"))
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            logging.warning("[pushraker] spool image failed: %s", e)
            return None

    async def _send_parts(
        self, rid: str,
        messages: Optional[List[Tuple[str, str, List[Dict[str, Any]]]]],
        packer: Optional[Tuple[Any, ...]] = None
    ) -> bool:
        """Send one or more messages, each with its own spool picture, to
        each of the row's destinations, in their channels' turn. Either
        fixed `messages` (title, body, image rows), or `packer` = (title,
        header, issues, text_fn, row_fn), split per channel to fit."""
        row = self.rows.get(rid)
        if row is None:
            return False
        kind, dests = row
        tickets = [self._reserve(d.channel, rid) for d in dests]
        paths: Dict[Tuple[int, ...], Optional[str]] = {}
        try:
            extra = await self._data_text(rid)
            per_dest: List[List[Tuple[str, str, Optional[str]]]] = []
            for d in dests:
                if packer is None:
                    msgs = [(t, self._lines(b, extra), rows)
                            for t, b, rows in messages or []]
                    keyed = [((k,), m) for k, m in enumerate(msgs)]
                else:
                    title, header, issues, text_fn, row_fn = packer
                    parts = self._pack(d.channel, header, issues, text_fn)
                    keyed = []
                    for n, (idx, body) in enumerate(parts, 1):
                        t = title if len(parts) == 1 \
                            else f"{title} ({n}/{len(parts)})"
                        keyed.append((tuple(idx), (
                            t, self._lines(body, extra),
                            [row_fn(issues[i]) for i in idx])))
                out = []
                for key, (t, b, rows) in keyed:
                    path = None
                    if kind == "spool":
                        if key not in paths:
                            drawn = await self.eventloop.run_in_thread(
                                self._render_rows, rows)
                            paths[key] = self._named_copy(drawn, t)
                            if drawn and paths[key] != drawn:
                                shutil.rmtree(os.path.dirname(drawn),
                                              ignore_errors=True)
                        path = paths[key]
                    out.append((t, b, path))
                per_dest.append(out)
            report: List[Dict[str, Any]] = []
            results = self._outcomes(rid, dests, await asyncio.gather(*(
                self._deliver(rid, d, t, msgs, report)
                for d, t, msgs in zip(dests, tickets, per_dest)),
                return_exceptions=True))
            self._console_report(report)
        finally:
            for t in tickets:
                await t.gate.done(t.ticket)
            for p in paths.values():
                if p:
                    shutil.rmtree(os.path.dirname(p), ignore_errors=True)
        return all(results)


def load_component(config: ConfigHelper) -> PushNotifications:
    return PushNotifications(config)
