# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The Snapmaker U1 (with the extended firmware): what its Klipper adds
beyond the standard objects. The firmware-side harness (the install step,
the nginx alias, the Firmware Config entry) lives in the firmware's own
repository, overlay 42-feature-pushraker."""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence

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
    # Chosen on the screen when a print job starts (print_task_config.py);
    # while each runs, machine_state_manager's action_code is BED_PREHEATING
    # (257), BED_PRESCANNING (258), BED_LEVELING (256),
    # EXTRUDER<n>_FLOW_CALIBRATING (320 + toolhead) or SHAPER_CALIBRATING
    # (384), back to IDLE (0) after each (seen on a U1: 257, 258, 256, 0;
    # 323, 0)
    preprint_calibrations = {
        "auto_bed_leveling": "Bed levelling",
        "flow_calibrate": "Extrusion flow calibration (pressure advance)",
        "shaper_calibrate": "Input shaper calibration",
    }
    preprint_codes = {
        256: ("auto_bed_leveling", None), 257: ("auto_bed_leveling", None),
        258: ("auto_bed_leveling", None),
        320: ("flow_calibrate", 0), 321: ("flow_calibrate", 1),
        322: ("flow_calibrate", 2), 323: ("flow_calibrate", 3),
        384: ("shaper_calibrate", None),
    }
    rows_unavailable: Sequence[str] = ()
    # The stock PRINT_END / CANCEL_PRINT raise Z by 5 mm below 265 mm; the
    # lift stays inside that
    lift_ceiling = 265
    # Snapmaker's own objects the picker leaves out: state machines and
    # readers nobody wants in a notification; the purifier and multiACE
    # are offered with friendly names instead (catalogue below)
    hidden_objects: Sequence[str] = (
        "print_task_config", "machine_state_manager", "homing_xyz_override",
        "inductance_coil", "fm175xx_reader", "purifier", "ace",
        "filament_detect", "extruder_calibration", "flow_calibrator",
        "defect_detection", "filament_parameters", "filament_protocol",
        "filament_entangle_detect", "filament_feed", "auto_screws_tilt_adjust",
        "error_mcu", "garbage_collection", "extruder_config_bak",
    )
    LABELS = {"heater_bed": "Heated bed", "temperature_sensor cavity": "Cavity",
              "extruder": "Toolhead 1 nozzle", "extruder1": "Toolhead 2 nozzle",
              "extruder2": "Toolhead 3 nozzle", "extruder3": "Toolhead 4 nozzle"}

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        # print_task_config (the toolhead map) is Snapmaker's own
        return "print_task_config" in objects

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        self.extruders = ["extruder", "extruder1", "extruder2", "extruder3"]

    def label(self, obj: str) -> Optional[str]:
        return self.LABELS.get(obj)

    def job_values(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """job.toolheads: the toolheads the job uses, with their filaments
        ("Toolhead 1 (PLA Basic White), Toolhead 3 (PLA Matte Black)");
        with multiACE, each filament's slot (job.filamentN.slot) and
        job.slots."""
        fils: List[Dict[str, Any]] = ctx.get("filaments") or []
        emap = ctx.get("map") or []
        if ctx.get("sample"):
            for f in fils:
                f.setdefault("toolhead", 1)
            return {"toolheads": ", ".join(
                f"Toolhead {f['toolhead']} ({f.get('name') or 'n/a'})"
                for f in fils)}
        heads = []
        sources = {}
        for h, src in ((ctx.get("ace") or {}).get("head_source") or {}).items():
            if isinstance(src, dict) and src.get("ace_index") is not None \
                    and src.get("slot") is not None:
                sources[str(h)] = (int(src["ace_index"]), int(src["slot"]))
        for f in fils:
            t = f.get("index")
            if not f.get("used", True):
                continue
            ch = emap[t] if isinstance(t, int) and t < len(emap) \
                and isinstance(emap[t], int) else t
            if not isinstance(ch, int):
                continue
            f["toolhead"] = ch + 1
            heads.append(f"Toolhead {ch + 1} ({f.get('name') or 'n/a'})")
            if str(ch) in sources:
                a, s = sources[str(ch)]
                f["slot"] = f"ACE {a + 1} slot {s + 1}"
        out: Dict[str, Any] = {"toolheads": ", ".join(heads) or None}
        if sources:
            out["slots"] = ", ".join(
                f["slot"] for f in fils if f.get("slot")) or None
        return out

    def catalogue(self, ctx: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        if "purifier" in self.objects:
            out["fans"] = [
                {"label": "Exhaust fan", "object": "purifier", "values": [
                    {"label": "Speed", "token": "{purifier.exhaust_fan.speed|pct}"}]},
                {"label": "Inner fan", "object": "purifier", "values": [
                    {"label": "Speed", "token": "{purifier.inner_fan.speed|pct}"},
                    {"label": "RPM", "token": "{purifier.inner_fan_rpm|0}"}]},
            ]
        job = [{"label": "Toolheads used", "object": "job", "values": [
            {"label": "Toolheads", "token": "{job.toolheads}"},
            {"label": "Filament 1 toolhead", "token": "{job.filament1.toolhead}"}]}]
        if "ace" in self.objects:
            job.append({"label": "multiACE", "object": "job", "values": [
                {"label": "Slots used", "token": "{job.slots}"},
                {"label": "Filament 1 slot", "token": "{job.filament1.slot}"}]})
        out["job"] = job
        return out
