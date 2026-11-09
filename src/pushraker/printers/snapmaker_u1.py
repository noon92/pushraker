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
    # The touchscreen's "Remaining": the slicer's estimate scaled by the
    # share of the file still to print (confirmed against screen snapshots)
    time_methods = {"snapmaker": (
        "Snapmaker", "What the printer's touchscreen shows: the slicer's "
        "estimated print time multiplied by the share of the file still to "
        "print. Ignores how fast the print actually runs, so it drifts on "
        "prints that run slower than sliced; Blend was more accurate on "
        "tested prints.")}
    time_default = "snapmaker"
    HINTS = {"temperature_sensor cavity": "cavity",
             "dragonbreath": "chamber heater"}

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        # print_task_config (the toolhead map) is Snapmaker's own
        return "print_task_config" in objects

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        self.extruders = ["extruder", "extruder1", "extruder2", "extruder3"]

    def hint(self, obj: str) -> Optional[str]:
        return self.HINTS.get(obj)

    def filament_extruder(self, index: int, ctx: Dict[str, Any]
                          ) -> Optional[int]:
        # The toolhead map chosen on the screen at print start
        emap = list((ctx.get("ptc") or {}).get("extruder_map_table")
                    or ctx.get("map") or [])
        if index < len(emap) and isinstance(emap[index], int):
            return emap[index]
        return index if index < 4 else None

    def extruder_values(self, ch: int, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """print_task_config keeps the filament of each toolhead (read by
        its RFID tag, or set on the screen); multiACE's head_source says
        which slot feeds it."""
        ptc = ctx.get("ptc") or {}

        def field(key: str) -> Optional[str]:
            v = list(ptc.get(key) or [])
            val = str(v[ch]).strip() if 0 <= ch < len(v) else ""
            return val if val and val.upper() != "NONE" else None
        exist = list(ptc.get("filament_exist") or [])
        loaded = bool(exist[ch]) if 0 <= ch < len(exist) else None
        out: Dict[str, Any] = {}
        if loaded is not False:
            material, vendor = field("filament_type"), field("filament_vendor")
            sub = field("filament_sub_type")
            rgba = field("filament_color_rgba")
            if material or vendor:
                out["filament"] = {
                    "name": " ".join(x for x in (vendor, material, sub) if x),
                    "material": material, "vendor": vendor,
                    "color": f"#{rgba[:6].upper()}" if rgba else None}
            ids = list(ptc.get("filament_spool_id") or [])
            if 0 <= ch < len(ids) and ids[ch]:
                out["spool_id"] = ids[ch]
        src = ((ctx.get("ace") or {}).get("head_source") or {}).get(str(ch))
        if isinstance(src, dict) and src.get("ace_index") is not None \
                and src.get("slot") is not None:
            out["slot"] = f"ACE {int(src['ace_index']) + 1} slot " \
                f"{int(src['slot']) + 1}"
        return out

    def extruder_catalogue(self, obj: str, spoolman: bool
                           ) -> List[Dict[str, Any]]:
        from .. import templating as tp
        fil = [tp.node("name", token=f"{obj}.filament.name"),
               tp.node("material", token=f"{obj}.filament.material"),
               tp.node("vendor", token=f"{obj}.filament.vendor"),
               tp.node("color", token=f"{obj}.filament.color")]
        if spoolman:
            fil.append(tp.node(
                "grams_left", info="Filament left on the spool, from "
                "Spoolman.", token=f"{obj}.filament.grams_left",
                formats=tp.format_options(640.0)))
        out = [tp.node("filament", hint="loaded now · generated",
                       info="The filament loaded in this toolhead, as the "
                       "printer knows it (its RFID tag or the screen). "
                       "Generated by pushraker.", children=fil)]
        if "ace" in self.objects:
            out.append(tp.node("slot", hint="multiACE · generated",
                               info="The multiACE slot feeding this "
                               "toolhead. Generated by pushraker.",
                               token=f"{obj}.slot"))
        return out

    def fan_catalogue(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        from .. import templating as tp
        if "purifier" not in self.objects:
            return []
        st = state.get("purifier") or {}
        ex = (st.get("exhaust_fan") or {}).get("speed")
        inner = (st.get("inner_fan") or {}).get("speed")
        speed = "Fan speed, 0-100 %."
        return [tp.node("purifier", hint="exhaust and inner fans", children=[
            tp.node("exhaust_fan", children=[tp.node(
                "speed", info=speed, token="purifier.exhaust_fan.speed",
                formats=tp.format_options(ex if ex is not None else 0.4,
                                          True))]),
            tp.node("inner_fan", children=[tp.node(
                "speed", info=speed, token="purifier.inner_fan.speed",
                formats=tp.format_options(inner if inner is not None
                                          else 1.0, True))]),
            tp.node("inner_fan_rpm", info="The inner fan's measured speed.",
                    token="purifier.inner_fan_rpm"),
        ])]

    def left_seconds(self, method: str, ctx: Dict[str, Any]) -> Optional[float]:
        if method == "snapmaker" and ctx.get("estimated", 0) > 0:
            return ctx["estimated"] * (1. - ctx.get("sd_progress", 0.))
        return None
