# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The Snapmaker U1 (with the extended firmware): what its Klipper adds
beyond the standard objects. The firmware-side harness (the install step,
the nginx alias, the Firmware Config entry) lives in the firmware's own
repository, overlay 42-feature-notifications."""

from __future__ import annotations
from typing import Sequence

from . import Printer


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
    # The stock PRINT_END / CANCEL_PRINT raise Z by 5 mm below 265 mm; the
    # lift stays inside that
    lift_ceiling = 265

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        # print_task_config (the toolhead map) is Snapmaker's own
        return "print_task_config" in objects

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        self.extruders = ["extruder", "extruder1", "extruder2", "extruder3"]
