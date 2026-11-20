# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Creality's own firmware (the Hi; its CFS is the `box` object, the spool
holder's RFID reader `filament_rack`). Its layers need nothing here: the
generic Printer reads virtual_sdcard's layer fields, which Creality's
Klipper adds.

The model: Creality names the printer "Creality <model>-<last 4 hex of
the MAC>" (the Hi: "Creality Hi-78E7"), which Klipper reports as its
hostname. Creality's own model code (system_config.json, "F018" on the
Hi) isn't a name people know.
"""

from __future__ import annotations
import re
from typing import Sequence

from . import Printer

RX_HOSTNAME = re.compile(r"(Creality [A-Za-z0-9 +]+?)-[0-9A-Fa-f]{4}")


class Creality(Printer):
    name = "creality"
    model_name = "Creality printer"

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        return "box" in objects or "filament_rack" in objects

    def model(self, hostname: str = "") -> str:
        m = RX_HOSTNAME.fullmatch(hostname.strip())
        return m.group(1) if m else self.model_name
