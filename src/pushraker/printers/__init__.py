# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""What differs between printers: the harness side of the package.

The component works on any Moonraker printer from Klipper's standard
objects (print_stats, virtual_sdcard, exclude_object, idle_timeout, the
heater_generic and tmc objects). Everything beyond that is a Printer here,
chosen from the printer's object list at Klipper's ready: the Snapmaker
U1's firmware adds objects of its own (print_task_config with the toolhead
map and the refill rules, machine_state_manager with the touchscreen
calibrations, its exception manager with coded faults, the purifier's
chamber wait, power-loss recovery files, the chamber sensor's name).
Another printer's specifics go in another module of this package (one
file per printer), listed in PRINTERS below, not in the component.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import preprint


class Frame(bytes):
    """A JPEG from snapshot() that was taken earlier, not just now:
    `taken` is when (time.time()). The notification then says how old it
    is, when it's old."""
    taken: Optional[float] = None

    def __new__(cls, data: bytes, taken: Optional[float]) -> "Frame":
        obj = super().__new__(cls, data)
        obj.taken = taken
        return obj


class Printer:
    """A plain Moonraker printer."""
    name = "generic"
    # The printer maker's page of error codes, linked from fault
    # notifications ("Error Index") unless the harness names another
    error_index_url = ""
    # Minutes the printer usually spends preparing before it prints
    # (heating, homing, the default calibrations): added to the ETA until
    # printing begins, and the ETA is then shown as approximate. A rough
    # figure: it changes with the macros and the calibrations chosen.
    # 0: not known.
    prep_minutes = 0
    # The printer's model as people know it ("Snapmaker U1"), for backups'
    # "from"; empty: the profile's name stands in
    model_name = ""
    # Moonraker event carrying the printer's coded faults (None: only
    # print_stats and Klipper shutdowns report faults)
    exception_event: Optional[str] = None
    # Where the printer's own texts for fault codes are (json, "<id>-
    # <index>-<code>": "<title>_<details>")
    exception_text_paths: Sequence[str] = ()
    # The exception manager module id of filament runouts (see the U1)
    runout_exception_id: Optional[int] = None
    # machine_state_manager.main_state values of the touchscreen
    # calibrations, and the states that mean idle and abnormal
    calibrations: Dict[int, str] = {}
    msm_idle = 0
    msm_abnormal = 7
    # action_code values of the steps that wait for a person: the toolhead
    # to brush, by number
    manual_clean: Dict[int, int] = {}
    # The chamber temperature sensor's object name, and whether the
    # printer's own chamber wait (purifier) exists
    chamber_sensor: str = ""
    action_preheat_chamber: Optional[int] = None
    # Power-loss recovery: where the interrupted print's record is kept
    power_loss_env_paths: Sequence[str] = ()
    # The extruder objects, in toolhead order (nozzle checks for refills)
    extruders: List[str] = ["extruder"]
    # The printer's own toolhead map and refill (print_task_config): with
    # it, runouts can be refilled from another toolhead
    toolhead_map = False
    # Pre-print calibrations a print job can be started with (chosen on
    # the printer when the job starts): the flag in print_task_config ->
    # its name; and the action_code values (machine_state_manager) while
    # each runs, -> (the flag, the toolhead index for per-toolhead ones,
    # else None). Empty: the printer has none (no line, no checkbox)
    preprint_calibrations: Dict[str, str] = {}
    preprint_codes: Dict[int, Tuple[str, Optional[int]]] = {}
    # The print-end snapshot lift keeps Z at or below this; 0 means 5 mm
    # under the printer's axis maximum (the macro file reads it)
    lift_ceiling = 0
    # Notification rows that need something this printer doesn't have
    # (the page leaves them out): touchscreen calibrations, the toolhead
    # map's filament sensing and refills, an MMU, power-loss recovery,
    # coded warnings
    rows_unavailable: Sequence[str] = ("calibration", "filament_change",
                                       "spool_switch", "slot_empty",
                                       "power_loss", "warning",
                                       "preprint_done")
    # Notification data (templating.py): objects of this printer's own
    # that the picker shouldn't list (typed tokens still work), beyond
    # Klipper's internals
    hidden_objects: Sequence[str] = ()
    # Time-left methods of this printer's own (id -> (label, help); see
    # component.TIME_METHODS for the generic ones and left_seconds for the
    # computing), and the method this printer defaults to
    time_methods: Dict[str, Tuple[str, str]] = {}
    time_default = "blend"
    # Status fields adjust_status reads, subscribed and queried with the
    # component's ({object: [fields]}); Klipper leaves out the fields an
    # object doesn't have. virtual_sdcard's layer and layer_count: Creality's
    # Klipper fork (the Hi, confirmed; the K1 family probably, unconfirmed)
    # counts layers there instead of in print_stats.info
    status_fields: Dict[str, List[str]] = {
        "virtual_sdcard": ["layer", "layer_count"]}
    # The printer's own web page (its web UI), for the Printer URL: a URL
    # with "{host}" for the printer's address as the browser reaches it
    # ("http://{host}:4408/"), and what to call it on the page. "": none
    # declared, the web pages found on the machine are offered instead
    # (pushraker.discover.web_pages)
    web_page = ""
    web_page_name = ""
    # Why this printer's own camera picture can be old (snapshot() giving a
    # Frame taken earlier): the end of the text's "Camera image from 74 s
    # ago: ..." line; "" says only how old
    frame_note = ""

    def __init__(self, objects: Sequence[str]) -> None:
        self.objects = list(objects)
        # X and Y stepper drivers that report their own temperature
        # (TMC2240 and TMC5160 have an ADC; others only report a flag)
        self.drivers: Dict[str, str] = {
            o: o.rsplit("_", 1)[-1].upper() for o in self.objects
            if o.startswith(("tmc2240 ", "tmc5160 "))
            and o.endswith(("stepper_x", "stepper_y"))}
        self.extruders = [e for e in ("extruder", "extruder1", "extruder2",
                                      "extruder3", "extruder4", "extruder5")
                          if e in self.objects] or ["extruder"]
        # Whether print_stats.info comes from the slicer (SET_PRINT_STATS_
        # INFO; Klipper only sets current_layer together with total_layer)
        self._slicer_layers = False
        # Pre-print calibrations from stock Klipper (pushraker.preprint),
        # for a profile without detection of its own: bed mesh, Z tilt,
        # gantry level, as far as the printer has them
        self.generic_preprint = not self.preprint_calibrations and \
            preprint.available(self.objects)
        if self.generic_preprint:
            fields = dict(self.status_fields)
            for obj, f in preprint.STATUS_FIELDS.items():
                if obj in self.objects:
                    fields[obj] = list(dict.fromkeys(
                        list(fields.get(obj, [])) + f))
            self.status_fields = fields
            self.rows_unavailable = tuple(
                r for r in self.rows_unavailable if r != "preprint_done")

    def has(self, obj: str) -> bool:
        return obj in self.objects

    def cameras(self) -> Dict[str, Dict[str, str]]:
        """The printer's own cameras that neither Moonraker nor the
        generic discovery (pushraker.discover) can find: {name:
        {"snapshot": a URL giving a JPEG, "stream": the live view's path
        or URL}}. None on a plain printer."""
        return {}

    def snapshot(self, name: str, ctx: Dict[str, Any]
                 ) -> Tuple[Optional[bytes], str]:
        """A JPEG from one of this printer's own cameras ("printer:<name>"
        addresses from cameras()), or why there's none. `ctx`: "state"
        (print_stats.state), "since_end" (seconds since a print ended, or
        None). Blocking: called in a thread. A picture taken earlier
        comes as a Frame, with when."""
        return None, "this printer has no camera of its own"

    def model(self, hostname: str = "") -> str:
        """The most specific model name known (`hostname`: Klipper's,
        from printer.info)."""
        return self.model_name or self.name

    def missing_rows(self) -> Set[str]:
        """Rows for hardware this printer doesn't have, from Klipper's
        objects at Klippy ready (one added later shows after a restart):
        left off the page whatever their setting, which stays as saved.
        One table for every printer; empty before Klipper is known."""
        o = self.objects
        if not o:
            return set()
        chamber = any(x.startswith("heater_generic ") for x in o) or \
            "purifier" in o or "dragonbreath" in o
        needs = {
            # a chamber target: any heater_generic (M141/M191), Snapmaker's
            # chamber wait (its purifier), a DragonBreath
            "chamber_waiting": chamber,
            "chamber_reached": chamber,
            "chamber_fault": chamber,
            # X/Y drivers that report their own temperature
            "driver_hot": bool(self.drivers),
            "bed_cooled": "heater_bed" in o,
        }
        return {rid for rid, ok in needs.items() if not ok}

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        """Whether this is the printer behind an object list."""
        return False

    # -- notification data (templating.py) --

    def hint(self, obj: str) -> Optional[str]:
        """A few words shown next to one of this printer's objects in the
        picker ("cavity"); the object keeps its Klipper name."""
        return None

    def filament_extruder(self, index: int, ctx: Dict[str, Any]
                          ) -> Optional[int]:
        """The extruder (0 = extruder, 1 = extruder1...) that prints the
        slicer's filament `index` (0 = the slicer's filament 1): T<n> on a
        plain printer; a printer with a toolhead map looks it up."""
        return index

    def extruder_values(self, ch: int, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """What pushraker adds to extruder <ch>: {"filament": {"name",
        "material", "vendor", "color"} of the filament loaded, "slot": the
        MMU slot feeding it, "spool_id": its Spoolman spool}. `ctx`:
        status (the component's subscribed objects), ptc, map, ace. Empty
        when the printer doesn't know."""
        return {}

    def extruder_catalogue(self, obj: str, spoolman: bool
                           ) -> List[Dict[str, Any]]:
        """Picker entries for what extruder_values gives (templating.node
        trees under the extruder `obj`); none on a plain printer."""
        return []

    def fan_catalogue(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Picker entries for fans of this printer's own objects (the
        objects' current status in `state`)."""
        return []

    def adjust_status(self, data: Dict[str, Any],
                      status: Dict[str, Dict[str, Any]]) -> None:
        """Fill in a standard value the printer keeps somewhere else (in
        place, in `status`: the component's merged status), after each
        update `data` is merged. The values the notifications show stay
        the standard ones; only where they come from differs.

        Layers: Creality's Klipper fork counts them in virtual_sdcard
        (`layer`: +1 at each layer marker in the file, ";LAYER_CHANGE" and
        the like, the first at the start of layer 1, 0 before it and while
        idle, restored after a power-loss resume; `layer_count`: the file's
        layer count, set when the print starts; both zeroed when the print
        ends, is cancelled or fails), and Creality Print doesn't write
        SET_PRINT_STATS_INFO, so print_stats.info stays empty. Wherever
        those fields exist they are copied into print_stats.info, unless a
        slicer did write SET_PRINT_STATS_INFO: that one wins."""
        ps = status.get("print_stats")
        if ps is None:
            return
        new_info = (data.get("print_stats") or {}).get("info")
        if isinstance(new_info, dict):
            self._slicer_layers = new_info.get("total_layer") is not None
        if self._slicer_layers:
            return
        vsd = status.get("virtual_sdcard") or {}
        if "layer" not in vsd and "layer_count" not in vsd:
            return              # a standard virtual_sdcard
        try:
            total = int(vsd.get("layer_count") or 0)
            layer = int(vsd.get("layer") or 0)
        except (TypeError, ValueError):
            return
        if not total and not layer:
            if ps.get("state") in ("complete", "cancelled", "error"):
                return          # zeroed at the end: keep the job's last
            info: Dict[str, Any] = {"total_layer": None,
                                    "current_layer": None}
        else:
            info = {"total_layer": total or None,
                    "current_layer": min(layer, total) if total else layer}
        ps["info"] = info

    def left_seconds(self, method: str, ctx: Dict[str, Any]) -> Optional[float]:
        """Seconds left by one of this printer's own time_methods. `ctx`:
        estimated (the slicer's seconds), print_duration, progress (file
        progress over the G-code's byte range), sd_progress (Klipper's raw
        virtual_sdcard.progress), layer1_pd (print time when layer 1
        began, or None). None when it can't be computed."""
        return None


from .snapmaker_u1 import SnapmakerU1  # noqa: E402
from .creality import Creality  # noqa: E402

PRINTERS: Tuple[type, ...] = (SnapmakerU1, Creality)


def detect(objects: Sequence[str]) -> Printer:
    """The printer behind a Klipper object list: the first class that
    recognises it, else the generic one."""
    for cls in PRINTERS:
        if cls.matches(objects):
            return cls(objects)
    return Printer(objects)


# Events to listen for before the printer is known (a handler for an event
# that never fires costs nothing)
EXCEPTION_EVENTS = tuple(p.exception_event for p in PRINTERS
                         if p.exception_event)
