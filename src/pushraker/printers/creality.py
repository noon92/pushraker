# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Creality's own Klipper (the Hi; its CFS is the `box` object, the spool
holder's RFID reader `filament_rack`).

Layers: Creality Print doesn't write SET_PRINT_STATS_INFO, so Klipper's
print_stats.info stays empty. Creality's virtual_sdcard counts the layers
itself instead: `layer` goes up by one at each layer marker in the file
(";LAYER_CHANGE" and the like, the first one at the start of layer 1, 0
before it and while idle; restored after a power-loss resume), and
`layer_count` is the file's layer count, set when the print starts. Both
go back to 0 when a print ends, is cancelled or fails. They mean what
print_stats.info's current_layer and total_layer mean, so they are copied
there, unless the slicer did write SET_PRINT_STATS_INFO: that one wins.
"""

from __future__ import annotations
from typing import Any, Dict, List, Sequence

from . import Printer

ENDED = ("complete", "cancelled", "error")


class Creality(Printer):
    name = "creality"
    status_fields: Dict[str, List[str]] = {
        "virtual_sdcard": ["layer", "layer_count"]}

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        # Whether print_stats.info comes from the slicer (SET_PRINT_STATS_
        # INFO): Klipper only sets current_layer together with total_layer
        self._slicer_layers = False

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        return "box" in objects or "filament_rack" in objects

    def adjust_status(self, data: Dict[str, Any],
                      status: Dict[str, Dict[str, Any]]) -> None:
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
            return              # not Creality's virtual_sdcard
        try:
            total = int(vsd.get("layer_count") or 0)
            layer = int(vsd.get("layer") or 0)
        except (TypeError, ValueError):
            return
        if not total and not layer:
            if ps.get("state") in ENDED:
                return          # zeroed at the end: keep the job's last
            info: Dict[str, Any] = {"total_layer": None,
                                    "current_layer": None}
        else:
            info = {"total_layer": total or None,
                    "current_layer": min(layer, total) if total else layer}
        ps["info"] = info
