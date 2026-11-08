# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Notification data: text with {object.field|format} tokens, filled in
when a notification is sent.

A token names a Klipper object and one of its status fields, as Moonraker
reports them ({heater_bed.temperature}, {temperature_sensor cavity
.temperature}, {print_stats.info.current_layer}), or one of pushraker's
own values about the print job ({job.filaments}, {job.filament2.material},
{job.time_left}: the component computes them, see component._job_values).
An optional format follows a bar: |0 |1 |2 (decimals), |pct (a 0-1 fraction
as a percentage), |time (seconds as "1h 12m"). A value that isn't there
reads "n/a"; a token nobody can parse is left as typed. Rendering never
raises: a broken token must not stop a notification.

The catalogue (what the page's picker offers) is built here too, from
the printer's object list: known object types get friendly labels and
their useful fields, the rest is listed under Other with raw names.
"""

from __future__ import annotations
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NA = "n/a"
# {object.field}, {object with space.field.sub}, {job.filament2.name|fmt}
RX_TOKEN = re.compile(r"\{([A-Za-z_][^{}|]*?)(?:\|([A-Za-z0-9_]+))?\}")
MAX_DATA = 1500      # characters of rendered data per notification
JOB = "job"


class Token:
    __slots__ = ("raw", "obj", "path", "fmt")

    def __init__(self, raw: str, obj: str, path: List[str],
                 fmt: str) -> None:
        self.raw, self.obj, self.path, self.fmt = raw, obj, path, fmt


def parse(text: str) -> List[Token]:
    """The tokens of a text, in order (duplicates included)."""
    out: List[Token] = []
    for m in RX_TOKEN.finditer(text or ""):
        name, fmt = m.group(1).strip(), (m.group(2) or "").lower()
        if "." not in name:
            continue        # an object alone has no value
        obj, _, rest = name.partition(".")
        obj = obj.strip()
        path = [p.strip() for p in rest.split(".") if p.strip()]
        if not obj or not path:
            continue
        out.append(Token(m.group(0), obj, path, fmt))
    return out


def objects_needed(tokens: Sequence[Token]) -> Dict[str, List[str]]:
    """The Klipper objects and top-level fields the tokens read (job.*
    excluded): one Moonraker query fills them all."""
    need: Dict[str, List[str]] = {}
    for t in tokens:
        if t.obj == JOB:
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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{seconds}s"


def fmt_value(value: Any, fmt: str) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        items = [fmt_value(v, fmt) for v in value]
        return ", ".join(i for i in items if i != NA) or NA
    if isinstance(value, dict):
        return ", ".join(f"{k}: {fmt_value(v, fmt)}" for k, v in value.items()
                         ) or NA
    if isinstance(value, (int, float)):
        if fmt in ("0", "1", "2", "3"):
            return f"{float(value):.{fmt}f}"
        if fmt == "pct":
            return f"{float(value) * 100.:.0f}%"
        if fmt == "time":
            return format_duration(float(value))
        if fmt == "int":
            return str(int(round(float(value))))
        if isinstance(value, float):
            # Klipper's floats have noise: 60.00390625 reads as 60.0
            text = f"{value:.1f}"
            return text[:-2] if text.endswith(".0") and fmt == "" and \
                abs(value - round(value)) < 1e-9 else text
        return str(value)
    text = str(value)
    if fmt in ("0", "1", "2", "3", "pct", "time", "int"):
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
        try:
            value = resolve(toks[0])
        except Exception:
            value = None
        return fmt_value(value, toks[0].fmt)
    out = RX_TOKEN.sub(sub, text or "")
    if len(out) > MAX_DATA:
        out = out[:MAX_DATA - 1].rstrip() + "…"
    return out.strip()


# ---- catalogue -----------------------------------------------------------

# Object types the picker knows: category, label (None: the object's own
# name after the type), fields as (field, label, format)
THERMAL = "thermals"
FANS = "fans"
PRINT_JOB = "job"
FILAMENT = "filament"
OTHER = "other"
CATEGORIES = [
    (THERMAL, "🌡️ Thermals"), (FANS, "🌀 Fans"), (PRINT_JOB, "🖨️ Print job"),
    (FILAMENT, "🧵 Filament"), (OTHER, "❔ Other"),
]
HEATER_FIELDS = [("temperature", "Temperature", "0"),
                 ("target", "Target", "0"), ("power", "Power", "pct")]
FAN_FIELDS = [("speed", "Speed", "pct"), ("rpm", "RPM", "0")]
KNOWN: Dict[str, Tuple[str, Optional[str], List[Tuple[str, str, str]]]] = {
    "heater_bed": (THERMAL, "Heated bed", HEATER_FIELDS),
    "extruder": (THERMAL, "Nozzle", HEATER_FIELDS),
    "heater_generic": (THERMAL, None, HEATER_FIELDS),
    "temperature_sensor": (THERMAL, None,
                           [("temperature", "Temperature", "0")]),
    "temperature_host": (THERMAL, "Host CPU",
                         [("temperature", "Temperature", "0")]),
    "temperature_mcu": (THERMAL, None, [("temperature", "Temperature", "0")]),
    "temperature_fan": (FANS, None, FAN_FIELDS + [
        ("temperature", "Temperature", "0"), ("target", "Target", "0")]),
    "fan": (FANS, "Part cooling fan", FAN_FIELDS),
    "fan_generic": (FANS, None, FAN_FIELDS),
    "heater_fan": (FANS, None, FAN_FIELDS),
    "controller_fan": (FANS, None, FAN_FIELDS),
    "filament_switch_sensor": (FILAMENT, None, [
        ("filament_detected", "Filament detected", ""),
        ("enabled", "Enabled", "")]),
    "filament_motion_sensor": (FILAMENT, None, [
        ("filament_detected", "Filament detected", ""),
        ("enabled", "Enabled", "")]),
    "output_pin": (OTHER, None, [("value", "Value", "")]),
    "led": (OTHER, None, [("color_data", "Colour", "")]),
    "neopixel": (OTHER, None, [("color_data", "Colour", "")]),
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
    "garbage_collection",
}
HIDDEN_PREFIXES = ("gcode_macro ", "mcu", "tmc", "stepper_", "extruder_stepper ",
                   "manual_stepper ", "gcode_button ", "delayed_gcode ",
                   "duplicate_pin_override", "verify_heater ", "bed_mesh ",
                   "adxl345", "lis2dw", "display", "endstop_phase",
                   "homing_heaters", "output_pin _", "fan_generic _",
                   "temperature_sensor _")
# Fields of unknown objects that are worth offering: scalars, not lists
# or dicts, and not Klipper's bookkeeping
SKIP_FIELDS = {"is_active", "mcu_build_versions", "mcu_version",
               "last_stats", "pins", "config"}


def label_for(obj: str, printer_label: Callable[[str], Optional[str]]
              ) -> str:
    own = printer_label(obj)
    if own:
        return own
    kind, _, name = obj.partition(" ")
    known = KNOWN.get(kind)
    if known and known[1] and not name:
        return known[1]
    if kind == "extruder" and not name and obj != "extruder":
        return f"Nozzle {int(obj[8:]) + 1}"
    base = name or kind
    return base.replace("_", " ").strip().capitalize()


def catalogue(objects: Sequence[str], status: Dict[str, Dict[str, Any]],
              printer_label: Callable[[str], Optional[str]],
              printer_hidden: Sequence[str] = (),
              extra: Optional[Dict[str, List[Dict[str, Any]]]] = None
              ) -> List[Dict[str, Any]]:
    """The picker's categories: [{"id", "label", "items": [{"label",
    "object", "values": [{"label", "token"}]}]}]. `status` holds the
    current fields of the unknown objects (for Other). `extra` adds items
    to categories (the harness: the job's toolheads, an MMU's slots)."""
    items: Dict[str, List[Dict[str, Any]]] = {c: [] for c, _ in CATEGORIES}
    hidden = set(printer_hidden)
    for obj in objects:
        if obj in HIDDEN or obj in hidden or \
                obj.startswith(HIDDEN_PREFIXES) or obj.startswith("_"):
            continue
        kind = obj.partition(" ")[0]
        if kind == "extruder" or re.fullmatch(r"extruder\d+", obj):
            kind = "extruder"
        known = KNOWN.get(kind)
        label = label_for(obj, printer_label)
        if known:
            cat, _, fields = known
            values = [{"label": fl, "token": f"{{{obj}.{f}" + (f"|{fm}" if fm
                                                                else "") + "}"}
                      for f, fl, fm in fields]
        else:
            cat = OTHER
            st = status.get(obj) or {}
            values = [{"label": k.replace("_", " "), "token": f"{{{obj}.{k}}}"}
                      for k, v in sorted(st.items())
                      if k not in SKIP_FIELDS and not k.startswith("_")
                      and isinstance(v, (int, float, str, bool))]
            if not values:
                continue
        items[cat].append({"label": label, "object": obj, "values": values})
    for cat, more in (extra or {}).items():
        items.setdefault(cat, []).extend(more)
    out = []
    for cid, clabel in CATEGORIES:
        if items.get(cid):
            out.append({"id": cid, "label": clabel, "items": items[cid]})
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


def slicer_settings(tail: bytes) -> Dict[str, str]:
    """The "; key = value" settings in the end of a G-code file."""
    out: Dict[str, str] = {}
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
