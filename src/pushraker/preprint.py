# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Pre-print calibrations on any Klipper printer, for printers whose
profile has no detection of its own (the U1 has: printers/snapmaker_u1.py).

What ran between the job's start and its first layer, and how long each
took, from what stock Klipper reports:
- Bed mesh: BED_MESH_CALIBRATE ends with the console line "Mesh Bed
  Leveling Complete" (klippy/extras/bed_mesh.py). It starts by clearing
  the mesh (bed_mesh.profile_name goes to ""): the last clear before the
  line is its start. (The Creality Hi: its START_PRINT clears and reloads
  the mesh a few times before calibrating; the last clear is 16 s before
  the calibration's own start, 3 min 36 s against 3 min 20 s.)
- Z tilt / gantry level (Z_TILT_ADJUST, QUAD_GANTRY_LEVEL): `applied` goes
  False at the start and True when within tolerance (z_tilt.py,
  quad_gantry_level.py); without those updates, the console's "Making
  the following Z adjustments" and "Retries: n/m Probed points range: r
  tolerance: t" lines (done when r <= t).
Which ones will run isn't known in advance: a start macro decides (the Hi
levels the bed on prints started from Fluidd, and as the Creality app was
set on its own prints). So they're only reported once the first layer
starts.
"""

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

BED_MESH = "Bed mesh"
Z_TILT = "Z tilt"
GANTRY = "Gantry level"
OBJECTS = ("bed_mesh", "z_tilt", "quad_gantry_level")
STATUS_FIELDS = {"bed_mesh": ["profile_name"], "z_tilt": ["applied"],
                 "quad_gantry_level": ["applied"]}
MESH_DONE = "Mesh Bed Leveling Complete"
TILT_MADE = "Making the following Z adjustments"
RX_RETRIES = re.compile(r"Retries:\s*\d+/\d+\s+Probed points range:\s*"
                        r"([0-9.]+)\s+tolerance:\s*([0-9.]+)")


def available(objects: Sequence[str]) -> bool:
    """Whether the printer has anything this can see."""
    return any(o in objects for o in OBJECTS)


def duration_text(seconds: float) -> str:
    """45 s, 3 min 20 s, 12 min."""
    t = max(0, int(round(seconds)))
    if t < 60:
        return f"{t} s"
    m, s = divmod(t, 60)
    return f"{m} min {s} s" if s and m < 10 else f"{m} min"


class PreprintWatch:
    """One job's start routine. Feed it the status updates and console
    lines until the first layer; `ran()` lists what completed."""

    def __init__(self, objects: Sequence[str],
                 now: Optional[float] = None) -> None:
        self.gantry = "quad_gantry_level" in objects
        self.start = time.monotonic() if now is None else now
        self.done: List[Tuple[str, float]] = []
        self._mesh_start: Optional[float] = None
        self._tilt_start: Optional[float] = None
        self._tilt_made: Optional[float] = None

    def _add(self, name: str, began: Optional[float], now: float) -> None:
        began = self.start if began is None else began
        secs = max(0., now - began)
        for i, (n, s) in enumerate(self.done):
            if n == name:           # ran again: one entry, both times
                self.done[i] = (n, s + secs)
                return
        self.done.append((name, secs))

    def _last_end(self) -> Optional[float]:
        return getattr(self, "_ended", None)

    def _finish(self, name: str, began: Optional[float], now: float
                ) -> None:
        self._add(name, began if began is not None else self._last_end(),
                  now)
        self._ended = now

    def status(self, data: Dict[str, Any], now: Optional[float] = None
               ) -> None:
        now = time.monotonic() if now is None else now
        mesh = data.get("bed_mesh")
        if isinstance(mesh, dict) and "profile_name" in mesh \
                and not mesh["profile_name"]:
            self._mesh_start = now
        for obj in ("z_tilt", "quad_gantry_level"):
            st = data.get(obj)
            if not isinstance(st, dict) or "applied" not in st:
                continue
            if not st["applied"]:
                if self._tilt_start is None:
                    self._tilt_start = now
            elif self._tilt_start is not None or self._tilt_made:
                self._tilt_done(now)

    def _tilt_done(self, now: float) -> None:
        began = self._tilt_start
        self._tilt_start = self._tilt_made = None
        self._finish(GANTRY if self.gantry else Z_TILT, began, now)

    def console(self, line: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        text = line.strip()
        if text.startswith("// "):
            text = text[3:]
        if text.startswith(MESH_DONE):
            began, self._mesh_start = self._mesh_start, None
            self._finish(BED_MESH, began if began is not None
                         else self._last_end(), now)
        elif text.startswith(TILT_MADE):
            if self._tilt_start is None:
                self._tilt_start = self._last_end() or self.start
            self._tilt_made = now
        else:
            m = RX_RETRIES.search(text)
            if m and (self._tilt_start is not None or self._tilt_made):
                try:
                    within = float(m.group(1)) <= float(m.group(2))
                except ValueError:
                    within = False
                if within:
                    self._tilt_done(now)

    def first_layer(self, now: Optional[float] = None) -> None:
        """The first layer began: an adjustment made but never confirmed
        (no retries configured) counts as done when it was made."""
        if self._tilt_made is not None:
            self._tilt_done(self._tilt_made)

    def ran(self) -> List[str]:
        """["Bed mesh (3 min 20 s)", ...] in the order they finished."""
        return [f"{n} ({duration_text(s)})" for n, s in self.done]
