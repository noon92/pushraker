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
from typing import Any, Dict, List, Optional, Sequence, Tuple


class Printer:
    """A plain Moonraker printer."""
    name = "generic"
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

    def has(self, obj: str) -> bool:
        return obj in self.objects

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

    def left_seconds(self, method: str, ctx: Dict[str, Any]) -> Optional[float]:
        """Seconds left by one of this printer's own time_methods. `ctx`:
        estimated (the slicer's seconds), print_duration, progress (file
        progress over the G-code's byte range), sd_progress (Klipper's raw
        virtual_sdcard.progress), layer1_pd (print time when layer 1
        began, or None). None when it can't be computed."""
        return None


from .snapmaker_u1 import SnapmakerU1  # noqa: E402

PRINTERS: Tuple[type, ...] = (SnapmakerU1,)


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
