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
Another printer's specifics go in another class here, not in the
component.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Sequence, Tuple


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


class SnapmakerU1(Printer):
    name = "Snapmaker U1"
    exception_event = "snapmaker:exception_notification"
    exception_text_paths = ("/home/lava/resource/text/exception_en-GB.json",)
    runout_exception_id = 523
    # machine_state_manager.main_state values of the manual calibrations run
    # from the touchscreen (klippy/extras/machine_state_manager.py); the
    # calibration is complete when the state goes back to IDLE (0; PRINTING
    # is 1, seen in the Moonraker log as "machine state: {...}")
    calibrations = {2: "XYZ offset calibration", 3: "Bed levelling",
                    4: "Flow calibration", 5: "Input shaper calibration"}
    msm_idle = 0
    msm_abnormal = 7
    # action_code values (same file) of the steps that wait for a person:
    # MANUAL_CLEAN_EXTRUDER..3 (brush the nozzle, then tap the screen)
    manual_clean = {192: 1, 193: 2, 194: 3, 195: 4}
    chamber_sensor = "temperature_sensor cavity"
    action_preheat_chamber = 3
    power_loss_env_paths = (
        "/home/lava/printer_data/klippy/pl_print_file_env.json",
        "/home/lava/printer_data/config/snapmaker/pl_print_file_env.json",
    )
    toolhead_map = True

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        self.extruders = ["extruder", "extruder1", "extruder2", "extruder3"]


PRINTERS: Tuple[type, ...] = (SnapmakerU1,)


def detect(objects: Sequence[str]) -> Printer:
    """The printer behind a Klipper object list."""
    objs = set(objects)
    if "print_task_config" in objs:
        return SnapmakerU1(objects)
    return Printer(objects)


# Events to listen for before the printer is known (a handler for an event
# that never fires costs nothing)
EXCEPTION_EVENTS = tuple(p.exception_event for p in PRINTERS
                         if p.exception_event)
