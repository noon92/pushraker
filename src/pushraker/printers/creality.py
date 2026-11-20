# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Creality's own firmware (the Hi; its CFS is the `box` object, the spool
holder's RFID reader `filament_rack`). Its layers need nothing here: the
generic Printer reads virtual_sdcard's layer fields, which Creality's
Klipper adds.

The model, in this order:
1. Creality's model code in its own system_config.json (device_info.
   model_str), looked up in MODEL_CODES; the user can't change it.
2. Klipper's hostname, "Creality <model>-<last 4 hex of the MAC>" ("Creality
   Hi-78E7"), Creality's default; renamed in Creality's UI, it no longer
   says the model.
3. "Creality printer".
"""

from __future__ import annotations
import json
import re
from typing import Dict, Optional, Sequence

from . import Printer

RX_HOSTNAME = re.compile(r"(Creality [A-Za-z0-9 +]+?)-[0-9A-Fa-f]{4}")
# Where Creality's firmware keeps system_config.json: the Hi (and its
# OpenWrt kin) under /mnt/UDISK, the K1 family under /usr/data
SYSTEM_CONFIGS = ("/mnt/UDISK/creality/userdata/config/system_config.json",
                  "/usr/data/creality/userdata/config/system_config.json")
# Creality model codes (device_info.model_str). Only codes seen first-hand
# or in a reliable source, with where:
MODEL_CODES: Dict[str, str] = {
    # The Hi's own system_config.json (model 1013) and its Klipper config
    # folder F018_CR4NU200360C20, on noon's printer, 2026-11-21
    "F018": "Creality Hi",
}


class Creality(Printer):
    name = "creality"
    model_name = "Creality printer"
    system_configs: Sequence[str] = SYSTEM_CONFIGS

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        return "box" in objects or "filament_rack" in objects

    def model_code(self) -> Optional[str]:
        for path in self.system_configs:
            try:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f).get("device_info") or {}
            except (OSError, ValueError, AttributeError):
                continue
            code = str(info.get("model_str") or "").strip()
            if code:
                return code
        return None

    def model(self, hostname: str = "") -> str:
        code = self.model_code()
        if code and code in MODEL_CODES:
            return MODEL_CODES[code]
        m = RX_HOSTNAME.fullmatch(hostname.strip())
        return m.group(1) if m else self.model_name
