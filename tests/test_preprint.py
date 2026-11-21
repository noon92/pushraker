#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The generic pre-print watch (pushraker/preprint.py), fed the way the
Creality Hi did it on 2026-11-21 and the way stock Klipper reports Z tilt
and quad gantry levelling.

  python3 tests/test_preprint.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from pushraker import preprint as pp  # noqa: E402

_passes = 0
_failures = 0


def check(name, ok, detail=""):
    global _passes, _failures
    if ok:
        _passes += 1
        print(f"  ok    {name}")
    else:
        _failures += 1
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


def t(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def run_preprint_tests():
    print("Pre-print calibrations from stock Klipper")
    check("durations read naturally",
          [pp.duration_text(x) for x in (0, 45, 200, 600, 754)]
          == ["0 s", "45 s", "3 min 20 s", "10 min", "12 min"])
    check("available with bed_mesh, z_tilt or QGL only",
          pp.available(["bed_mesh"]) and pp.available(["quad_gantry_level"])
          and not pp.available(["extruder", "probe"]))

    # The Hi (Klipper's log, 2026-11-21): START_PRINT at 02:08:47, the
    # mesh cleared and reloaded around the nozzle clean and Z align, the
    # last clear at 02:15:37, the mesh done at 02:19:13
    w = pp.PreprintWatch(["bed_mesh", "z_tilt", "probe"], now=t("2:08:47"))
    w.status({"bed_mesh": {"profile_name": ""}}, t("2:09:04"))
    w.status({"bed_mesh": {"profile_name": "default"}}, t("2:09:10"))
    w.console("// probe at 99.000,266.000 is z=0.137500", t("2:10:04"))
    w.status({"bed_mesh": {"profile_name": ""}}, t("2:15:37"))
    w.console("// probe at 130.000,130.000 is z=0.05", t("2:15:57"))
    w.console("// Mesh Bed Leveling Complete", t("2:19:13"))
    w.status({"bed_mesh": {"profile_name": "default"}}, t("2:19:13"))
    w.first_layer(t("2:20:23"))
    check("the Hi: a bed mesh from its last clear to Klipper's line",
          w.ran() == ["Bed mesh (3 min 36 s)"], w.ran())

    # Z tilt with retries, then a mesh; `applied` updates
    w = pp.PreprintWatch(["bed_mesh", "z_tilt"], now=0)
    w.status({"z_tilt": {"applied": False}}, 10)
    w.console("// Making the following Z adjustments:", 30)
    w.console("// Retries: 0/5 Probed points range: 0.120000 tolerance: "
              "0.007500", 31)
    w.console("// Making the following Z adjustments:", 50)
    w.console("// Retries: 1/5 Probed points range: 0.004000 tolerance: "
              "0.007500", 51)
    w.status({"z_tilt": {"applied": True}}, 51)
    w.status({"bed_mesh": {"profile_name": ""}}, 60)
    w.console("// Mesh Bed Leveling Complete", 160)
    w.first_layer(200)
    check("Z tilt (retries until within tolerance), then the mesh",
          w.ran() == ["Z tilt (41 s)", "Bed mesh (1 min 40 s)"], w.ran())

    # Quad gantry level, no object updates, no retries configured: the
    # adjustment line alone, counted at the first layer
    w = pp.PreprintWatch(["quad_gantry_level"], now=0)
    w.console("// Making the following Z adjustments:", 25)
    check("not done before the first layer", w.ran() == [])
    w.first_layer(90)
    check("gantry level: done when its adjustment was made",
          w.ran() == ["Gantry level (25 s)"], w.ran())

    # Already applied from an earlier print, nothing run: nothing listed
    w = pp.PreprintWatch(["z_tilt", "bed_mesh"], now=0)
    w.status({"z_tilt": {"applied": True},
              "bed_mesh": {"profile_name": "default"}}, 1)
    w.console("// probe at 1,1 is z=0.1", 2)
    w.first_layer(50)
    check("nothing ran: nothing listed", w.ran() == [], w.ran())

    # Run twice (a macro meshes again): one entry, both times
    w = pp.PreprintWatch(["bed_mesh"], now=0)
    w.status({"bed_mesh": {"profile_name": ""}}, 0)
    w.console("// Mesh Bed Leveling Complete", 30)
    w.status({"bed_mesh": {"profile_name": ""}}, 40)
    w.console("Mesh Bed Leveling Complete", 70)
    check("twice: one entry, the times added",
          w.ran() == ["Bed mesh (1 min 0 s)"] or
          w.ran() == ["Bed mesh (1 min)"], w.ran())


if __name__ == "__main__":
    run_preprint_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
