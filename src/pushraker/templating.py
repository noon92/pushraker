# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Notification data: text with {object.field|format} tokens, filled in
when a notification is sent.

A token names a Klipper object and one of its status fields, exactly as
Moonraker reports them ({heater_bed.temperature}, {temperature_sensor
cavity.temperature}, {extruder1.nozzle_diameter}), a value pushraker adds
to an extruder ({extruder1.filament.material}), or a value pushraker
generates about the print job ({job.filament2.name},
{job.time.remaining}; see component._job_values).

After a bar, a format:
  numbers     |0 |1 |2 decimals; |pct a 0-1 fraction as a percentage
  durations   a pattern of D (days), H (hours), M (minutes), any case;
              a doubled letter is two digits; the largest unit in the
              pattern carries the total, the smaller ones what's left
              over; anything else is printed as is, and text in double
              quotes too ("h" prints h): |H:MM -> 1:05, |M -> 65
  clock times the same letters: |HH:MM -> 09:05; a later day adds +1d
A value that isn't there reads "n/a"; a token that can't be parsed is
left as typed. Rendering never raises: a broken token must not stop a
notification.
"""

from __future__ import annotations
import datetime
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NA = "n/a"
# {object.field}, {object with space.field.sub}, {job.filament2.name|fmt}
RX_TOKEN = re.compile(r"\{([A-Za-z_][^{}|]*?)(?:\|([^{}|]*))?\}")
# A token still holding the # placeholder in its name (job.filament#.name);
# a # in the format is a toolhead number (|Toolhead #)
RX_PLACEHOLDER = re.compile(r"\{[^{}|]*#[^{}|]*(?:\|[^{}]*)?\}")
MAX_DATA = 1500      # characters of rendered data per notification
JOB = "job"
EXTRUDERS = "extruders"     # every toolhead (pushraker's)
DECIMALS = ("0", "1", "2", "3")


class Token:
    __slots__ = ("raw", "obj", "path", "fmt")

    def __init__(self, raw: str, obj: str, path: List[str],
                 fmt: str) -> None:
        self.raw, self.obj, self.path, self.fmt = raw, obj, path, fmt


def parse(text: str) -> List[Token]:
    """The tokens of a text, in order (duplicates included)."""
    out: List[Token] = []
    for m in RX_TOKEN.finditer(text or ""):
        name, fmt = m.group(1).strip(), (m.group(2) or "").strip()
        if "." not in name:
            continue        # an object alone has no value
        obj, _, rest = name.partition(".")
        obj = obj.strip()
        path = [p.strip() for p in rest.split(".") if p.strip()]
        if not obj or not path:
            continue
        out.append(Token(m.group(0), obj, path, fmt))
    return out


def placeholders(text: str) -> List[str]:
    """Tokens that still hold #: the user has to put a number there."""
    return RX_PLACEHOLDER.findall(text or "")


def objects_needed(tokens: Sequence[Token]) -> Dict[str, List[str]]:
    """The Klipper objects and top-level fields the tokens read (job.*
    excluded): one Moonraker query fills them all."""
    need: Dict[str, List[str]] = {}
    for t in tokens:
        if t.obj in (JOB, EXTRUDERS):
            continue
        fields = need.setdefault(t.obj, [])
        if t.path[0] not in fields:
            fields.append(t.path[0])
    return need


def dig(value: Any, path: Sequence[str]) -> Any:
    """Follow a path into dicts and lists; None when it leads nowhere."""
    for p in path:
        if isinstance(value, dict):
            if p in value:
                value = value[p]
            else:
                return None
        elif isinstance(value, (list, tuple)):
            try:
                value = value[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value


class ToolName(str):
    """A toolhead's Klipper name (extruder, extruder1...) that formats by
    number: |Toolhead # -> Toolhead 1, |T# -> T1."""
    num: int

    def __new__(cls, name: str, num: int) -> "ToolName":
        out = str.__new__(cls, name)
        out.num = num
        return out


NAME_FORMATS = ("", "Toolhead #", "Tool #", "T#")


class Duration(float):
    """Seconds that read as a duration ("1h 12m" by default; patterns).
    Adding or subtracting durations (or seconds) gives a duration."""

    def __add__(self, other: Any) -> "Duration":
        return Duration(float(self) + float(other))

    __radd__ = __add__

    def __sub__(self, other: Any) -> "Duration":
        return Duration(float(self) - float(other))

    def __rsub__(self, other: Any) -> "Duration":
        return Duration(float(other) - float(self))


class Clock:
    """A clock time ("21:52", "+1d 09:05" on a later day; patterns)."""
    __slots__ = ("at", "days")

    def __init__(self, at: datetime.datetime, days: int) -> None:
        self.at, self.days = at, days


def format_duration(seconds: float) -> str:
    """57h 22m, 12m (the default for durations): hours, no days; whole
    minutes, seconds dropped."""
    t = max(0, int(seconds or 0))
    if t >= 3600:
        return "%dh %02dm" % (t // 3600, (t % 3600) // 60)
    return "%dm" % (t // 60)


# A pattern: "quoted text", words, and anything else. A word made only of
# D, H and M letters is a field (DD, H, HHMM...); any other word ("days",
# "min") prints as typed.
RX_PATTERN = re.compile(r'"([^"]*)"|([A-Za-z]+)|(.)', re.S)
RX_FIELDS = re.compile(r"[dD]+|[hH]+|[mM]+")
UNIT_MINUTES = {"d": 1440, "h": 60, "m": 1}


def _pattern_parts(pattern: str) -> List[Tuple[str, str]]:
    """[(kind, text)]: kind "unit" (a run of one letter) or "text"."""
    out: List[Tuple[str, str]] = []
    for m in RX_PATTERN.finditer(pattern):
        quoted, word, other = m.group(1), m.group(2), m.group(3)
        if quoted is not None:
            out.append(("text", quoted))
        elif word is not None and re.fullmatch(r"[dDhHmM]+", word):
            out += [("unit", r.group(0)) for r in RX_FIELDS.finditer(word)]
        else:
            out.append(("text", word if word is not None else other))
    return out


def apply_pattern(pattern: str, minutes: int,
                  units_total: bool = True) -> str:
    """D/H/M pattern over a number of minutes. units_total: the largest
    unit in the pattern carries the total (durations); otherwise each
    unit is the clock's own field (hours 0-23, minutes 0-59)."""
    parts = _pattern_parts(pattern)
    present = {t[0].lower() for k, t in parts if k == "unit"}
    values: Dict[str, int] = {}
    if units_total:
        left = minutes
        for u in ("d", "h", "m"):
            if u in present:
                values[u] = left // UNIT_MINUTES[u]
                left -= values[u] * UNIT_MINUTES[u]
    else:
        values = {"d": minutes // 1440, "h": (minutes // 60) % 24,
                  "m": minutes % 60}
    out = []
    for kind, text in parts:
        if kind == "unit":
            v = values.get(text[0].lower(), 0)
            out.append(f"{v:02d}" if len(text) >= 2 else str(v))
        else:
            out.append(text)
    return "".join(out)


def is_pattern(fmt: str) -> bool:
    return any(k == "unit" for k, _ in _pattern_parts(fmt))


def fmt_value(value: Any, fmt: str) -> str:
    if value is None or value == "":
        return NA
    low = fmt.lower()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, ToolName):
        return fmt.replace("#", str(value.num)) if "#" in fmt else str(value)
    if isinstance(value, Duration):
        minutes = int(max(0., float(value)) // 60)
        if fmt and is_pattern(fmt):
            return apply_pattern(fmt, minutes)
        return format_duration(float(value))
    if isinstance(value, Clock):
        clock = value.at.hour * 60 + value.at.minute
        text = apply_pattern(fmt, clock, units_total=False) \
            if fmt and is_pattern(fmt) else value.at.strftime("%H:%M")
        # Another day: +1d ahead; the date when it's past
        if value.days < 0:
            return value.at.strftime("%Y-%m-%d ") + text
        return (f"+{value.days}d " if value.days > 0 else "") + text
    if isinstance(value, (list, tuple)):
        sep = "\n" if low == "line" else ", "
        inner = "" if low in ("line", "comma") else fmt
        items = [fmt_value(v, inner) for v in value]
        return sep.join(i for i in items if i != NA) or NA
    if isinstance(value, dict):
        return ", ".join(f"{k}: {fmt_value(v, fmt)}" for k, v in value.items()
                         ) or NA
    if isinstance(value, (int, float)):
        if low in DECIMALS:
            return f"{float(value):.{low}f}"
        if low == "pct":
            return f"{float(value) * 100.:.0f}%"
        if isinstance(value, float):
            # Klipper's floats have noise: 60.00390625 reads as 60.0
            text = f"{value:.2f}".rstrip("0").rstrip(".")
            return text
        return str(value)
    text = str(value)
    if low in DECIMALS + ("pct",):
        try:
            return fmt_value(float(text), fmt)
        except ValueError:
            pass
    return text


def render(text: str, resolve: Callable[[Token], Any]) -> str:
    """The text with each token replaced by its formatted value. `resolve`
    returns the raw value (None for unavailable); any exception it raises
    counts as unavailable."""
    def sub(m: re.Match) -> str:
        toks = parse(m.group(0))
        if not toks:
            return m.group(0)
        if "#" in toks[0].obj or any("#" in p for p in toks[0].path):
            return NA
        try:
            value = resolve(toks[0])
        except Exception:
            value = None
        return fmt_value(value, toks[0].fmt)
    out = RX_TOKEN.sub(sub, text or "")
    if len(out) > MAX_DATA:
        out = out[:MAX_DATA - 1].rstrip() + "…"
    return out.strip()


# ---- the picker's tree ------------------------------------------------------

# The examples the picker shows for time formats: every field filled
EXAMPLE_DURATION = Duration((24 * 60 + 3 * 60 + 12) * 60.)    # 1d 3h 12m
TIME_FORMATS = ("", "D H:MM", "H:MM", "HH:MM", "M", 'D"d" H"h" MM"m"')
CLOCK_FORMATS = ("", "H:MM")


def format_options(value: Any, fraction: bool = False
                   ) -> List[Dict[str, str]]:
    out = _format_options(value, fraction)
    for o in out:
        o["filter"] = jinja_filter(value, o["fmt"])
    return out


def _format_options(value: Any, fraction: bool = False
                    ) -> List[Dict[str, str]]:
    """The formats the picker offers for a value, each with how it reads:
    [{"label": "219.8", "fmt": "1"}]. Empty: no choice to make. Time
    formats read on a fixed example (27h 12m; 09:05 tomorrow), so every
    field shows."""
    if isinstance(value, Duration):
        return [{"label": fmt_value(EXAMPLE_DURATION, f), "fmt": f}
                for f in TIME_FORMATS]
    if isinstance(value, Clock):
        example = Clock(datetime.datetime(2026, 1, 2, 9, 5), 1)
        return [{"label": fmt_value(example, f), "fmt": f}
                for f in CLOCK_FORMATS]
    names = [v for v in value if isinstance(v, ToolName)] \
        if isinstance(value, (list, tuple)) else \
        [value] if isinstance(value, ToolName) else []
    if names:
        out = [{"label": fmt_value(names, f) if isinstance(value, (list,
                tuple)) else fmt_value(names[0], f), "fmt": f}
               for f in NAME_FORMATS]
        if isinstance(value, (list, tuple)):
            out.append({"label": "one per line", "fmt": "line"})
        return out
    if isinstance(value, (list, tuple)) and any(
            isinstance(v, float) for v in value) and all(
            v is None or isinstance(v, (int, float)) for v in value):
        return [{"label": fmt_value(value, "pct") if f == "pct" else
                 fmt_value([round(v, int(f)) if f and v is not None else v
                            for v in value], ""), "fmt": f}
                for f in (("pct", "2") if fraction else ("", "0", "1"))] + \
            [{"label": "one per line", "fmt": "line"}]
    if isinstance(value, (list, tuple)):
        return [{"label": "a, b, c", "fmt": ""},
                {"label": "one per line", "fmt": "line"}]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if fraction:
        return [{"label": fmt_value(value, "pct"), "fmt": "pct"},
                {"label": fmt_value(round(value, 2), ""), "fmt": "2"}]
    if isinstance(value, int):
        return []
    # As Jinja's round() writes them (no trailing zeros)
    return [{"label": fmt_value(round(value, int(f)), ""), "fmt": f}
            for f in ("0", "1", "2")]


def jinja_name(token: str) -> str:
    """A picker token as the template names it: job.* and extruders.* as
    they are, a toolhead's added values under extruders, Klipper's objects
    under printer (printer["name with spaces"]); a queue position is
    job.queue.job<N>."""
    toks = parse("{" + token + "}")
    if not toks:
        return token
    t = toks[0]
    path = list(t.path)
    if t.obj == JOB and len(path) > 1 and path[0] == "queue" and \
            (path[1] == "#" or path[1].isdigit()):
        path[1] = "job" + path[1]
    if t.obj in (JOB, EXTRUDERS):
        return ".".join([t.obj] + path)
    if re.fullmatch(r"extruder\d*", t.obj) and path and \
            path[0] in ("filament", "slot"):
        return ".".join([EXTRUDERS, t.obj] + path)
    base = f'printer["{t.obj}"]' if not re.fullmatch(r"\w+", t.obj) \
        else f"printer.{t.obj}"
    return ".".join([base] + path)


def jinja_filter(value: Any, fmt: str) -> str:
    """A picker format as a Jinja filter: |round(1), |pct, |lines,
    |duration('H:MM'), |clock('H:MM'), |toolhead('T#')."""
    if not fmt or fmt == "comma":
        return ""
    if fmt == "line":
        return "|lines"
    if fmt == "pct":
        return "|pct"
    quoted = "'" + fmt.replace("'", "") + "'"
    sample = value[0] if isinstance(value, (list, tuple)) and value \
        else value
    if isinstance(sample, ToolName) or (isinstance(value, (list, tuple))
                                        and any(isinstance(v, ToolName)
                                                for v in value)):
        return f"|toolhead({quoted})"
    if isinstance(sample, Duration):
        return f"|duration({quoted})"
    if isinstance(sample, Clock):
        return f"|clock({quoted})"
    if fmt.isdigit():
        n = int(fmt)
        if isinstance(value, (list, tuple)):
            return f"|map('round', {n})|list"
        return "|round|int" if n == 0 else f"|round({n})"
    return ""


def node(name: str, hint: str = "", info: str = "",
         token: Optional[str] = None, formats: Optional[List[Dict[str, str]]]
         = None, children: Optional[List[Dict[str, Any]]] = None,
         placeholder: bool = False) -> Dict[str, Any]:
    """One entry of the picker's tree. `token` (without braces) is what
    clicking inserts (with a format, when `formats` offers some); an
    entry with children or formats opens instead."""
    out: Dict[str, Any] = {"name": name}
    if hint:
        out["hint"] = hint
    if info:
        out["info"] = info
    if token:
        out["token"] = token
        out["expr"] = jinja_name(token)
    if formats:
        out["formats"] = formats
    if children:
        out["children"] = children
    if placeholder:
        out["placeholder"] = True
    return out


# Klipper's own fields of the object types the picker knows: (field, hint,
# info, the value is a 0-1 fraction)
HEATER_FIELDS = [
    ("temperature", "now", "", False),
    ("target", "set", "0 when off.", False),
    ("power", "heater power", "", True)]
EXTRUDER_FIELDS = [
    ("temperature", "nozzle, now", "", False),
    ("target", "nozzle, set", "0 when off.", False),
    ("power", "heater power", "", True),
    ("nozzle_diameter", "mm", "", False)]
# Values whose decimals aren't a choice (a 0.4 nozzle is never "0")
NO_FORMATS = {"nozzle_diameter"}
# Temperatures: the picker offers °C after their decimals
DEGREE_FIELDS = {"temperature", "target", "nozzle_temperature",
                 "bed_temperature"}
SENSOR_FIELDS = [("temperature", "now", "", False)]
FAN_FIELDS = [
    ("speed", "", "", True),
    ("rpm", "", "Fans with a tachometer only.", False)]
THERMAL_TYPES = {"heater_bed": HEATER_FIELDS, "heater_generic": HEATER_FIELDS,
                 "temperature_sensor": SENSOR_FIELDS,
                 "temperature_host": SENSOR_FIELDS,
                 "temperature_mcu": SENSOR_FIELDS}
FAN_TYPES = {"fan": FAN_FIELDS, "fan_generic": FAN_FIELDS,
             "heater_fan": FAN_FIELDS, "controller_fan": FAN_FIELDS,
             "temperature_fan": FAN_FIELDS + [
                 ("temperature", "now", "", False),
                 ("target", "set", "", False)]}
GENERIC_HINTS = {"heater_bed": "heated bed", "fan": "part cooling fan",
                 "temperature_host": "host computer"}
FIELD_INFO = {
    "filament_detected": "yes / no.",
    "enabled": "Sensor switched on: yes / no.",
    "value": "0-1 (1 = fully on).",
}
# Klipper's and Moonraker's internals: never offered (typing a token for
# one still works)
HIDDEN = {
    "configfile", "webhooks", "gcode", "gcode_move", "toolhead",
    "motion_report", "idle_timeout", "print_stats", "virtual_sdcard",
    "display_status", "exclude_object", "pause_resume", "query_endstops",
    "system_stats", "manual_probe", "probe", "bed_mesh", "save_variables",
    "firmware_retraction", "input_shaper", "resonance_tester", "bed_screws",
    "screws_tilt_adjust", "quad_gantry_level", "z_tilt", "delta_calibrate",
    "skew_correction", "force_move", "respond", "homing_override",
    "stepper_enable", "menu", "board_pins", "gcode_arcs", "pid_calibrate",
    "garbage_collection", "heaters",
}
HIDDEN_PREFIXES = ("gcode_macro ", "mcu", "stepper_", "tmc", "extruder_stepper ",
                   "manual_stepper ", "gcode_button ", "delayed_gcode ",
                   "duplicate_pin_override", "verify_heater ", "bed_mesh ",
                   "adxl345", "lis2dw", "display", "endstop_phase",
                   "homing_heaters", "output_pin _", "fan_generic _",
                   "temperature_sensor _", "_")
SKIP_FIELDS = {"mcu_build_versions", "mcu_version", "last_stats", "pins",
               "config"}


def hidden(obj: str, extra: Sequence[str] = ()) -> bool:
    return obj in HIDDEN or obj in extra or obj.startswith(HIDDEN_PREFIXES)


def fields_tree(obj: str, values: Dict[str, Any], prefix: str = "",
                depth: int = 0) -> List[Dict[str, Any]]:
    """An object's fields as Klipper names them, for Other: numbers,
    text and yes/no are values; nested dicts open; lists are left out."""
    out = []
    for k in sorted(values):
        v = values[k]
        if k in SKIP_FIELDS or k.startswith("_"):
            continue
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            if depth < 2:
                kids = fields_tree(obj, v, f"{path}.", depth + 1)
                if kids:
                    out.append(node(k, children=kids))
        elif isinstance(v, (int, float, str, bool)):
            out.append(node(k, info=FIELD_INFO.get(k, ""),
                            token=f"{obj}.{path}",
                            formats=format_options(v)))
    return out


# ---- the print job's slicer settings ------------------------------------------

# Orca/Bambu/Snapmaker Orca and PrusaSlicer write their settings as
# "; key = value" lines at the end of the file; per-filament values are
# separated by ";" (strings) or "," (numbers)
RX_SETTING = re.compile(rb"^; *([a-z_0-9]+) *= *(.*?)\s*$", re.M)
# Orca's bed type names (curr_bed_type) -> the plate temperature setting
BED_TYPES = {
    "cool plate": "cool_plate_temp", "cool plate (supertack)":
    "supertack_plate_temp", "engineering plate": "eng_plate_temp",
    "high temp plate": "hot_plate_temp", "textured pei plate":
    "textured_plate_temp", "textured cool plate": "textured_cool_plate_temp",
}


RX_USED = re.compile(rb"^; *(filament used \[(?:g|mm)\]) *= *(.*?)\s*$", re.M)


def slicer_settings(tail: bytes) -> Dict[str, str]:
    """The "; key = value" settings in the end of a G-code file, and the
    slicer's per-filament usage ("filament used [g]", "[mm]")."""
    out: Dict[str, str] = {}
    for m in RX_USED.finditer(tail or b""):
        out[m.group(1).decode()] = m.group(2).decode("utf-8", "replace")
    for m in RX_SETTING.finditer(tail or b""):
        try:
            out[m.group(1).decode()] = m.group(2).decode("utf-8", "replace")
        except Exception:
            continue
    return out


def split_values(text: str) -> List[str]:
    """A per-filament setting into its values. Orca quotes names with
    semicolons inside ("A;B" is one name)."""
    if not text:
        return []
    if '"' in text:
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
        if parts:
            return [p.replace('\\"', '"') for p in parts]
    sep = ";" if ";" in text else ","
    return [p.strip() for p in text.split(sep)]


def per_filament(settings: Dict[str, str], key_options: Sequence[str],
                 index: int) -> Optional[str]:
    """Value `index` (0-based) of the first setting present; the single
    value when the setting has only one (shared by every filament)."""
    for key in key_options:
        if key in settings:
            vals = split_values(settings[key])
            if not vals:
                return None
            if index < len(vals):
                return vals[index] or None
            return vals[0] if len(vals) == 1 else None
    return None


def bed_temp_keys(settings: Dict[str, str]) -> List[str]:
    """The bed temperature setting for the plate the job was sliced for
    (Orca), then the general ones."""
    keys: List[str] = []
    plate = (settings.get("curr_bed_type") or "").strip().lower()
    if plate in BED_TYPES:
        keys.append(BED_TYPES[plate])
    keys += ["hot_plate_temp", "bed_temperature", "textured_plate_temp",
             "cool_plate_temp", "eng_plate_temp", "first_layer_bed_temperature"]
    return keys


def number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return None
