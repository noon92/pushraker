#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""pushraker.values: the values a template uses, and whether this version
and this printer have them; and that the generated docs and values_map
match the Objects tree (tests/gen_values.py --check).

  python3 tests/test_values.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from pushraker import values as v  # noqa: E402
from pushraker import values_map  # noqa: E402

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


def run_values_tests():
    print("Template values: which a template uses, and who has them")
    r = v.referenced(["{{ job.time.remaining|duration }}"])
    check("an attribute chain through a filter", r == ["job.time.remaining"],
          r)
    r = v.referenced(["{% for e in extruders %}{{ e.temperature }}"
                      "{% endfor %}{% set j = job.file %}{{ j.x }}"])
    check("for and set: the loop's and the template's own names aren't "
          "values; what they come from is", r == ["job.file"], r)
    r = v.referenced(['"job.fake" {{ "job.str" }} {# job.comment #}'])
    check("text, strings and comments that look like values: none", r == [],
          r)
    r = v.referenced(['{{ printer["temperature_sensor cavity"].temperature '
                      '}} {{ job.filaments[0].name }}'])
    check("printer[...] names and number indexes",
          r == ["job.filaments.#.name",
                "printer.temperature_sensor cavity.temperature"], r)
    r = v.referenced(["{{ job.file.upper() }} {{ machine.name|lower }}"])
    check("a method of a value: the value", r == ["job.file",
                                                  "machine.name"], r)
    r = v.referenced(["{% if event.layer is defined and job.layer > 3 %}"
                      "{% endif %}", "{{ x.y }} {{ job }}", "{{ broken "])
    check("tests and comparisons; other roots, a root alone, text Jinja "
          "can't read: nothing", r == ["event.layer", "job.layer"], r)
    check("norm: numbers in names as #",
          v.norm("job.filament2.name") == "job.filament#.name"
          and v.norm("printer.extruder1.temperature")
          == "printer.extruder#.temperature"
          and v.norm("job.filaments.0") == "job.filaments.#")
    vmap = {"job.file": ("a", "b"), "job.time.remaining": ("a", "b"),
            "extruders.colors": ("a",)}
    missing, unknown = v.check(
        ["job.file", "job.time", "job.time.remaining.blend",
         "extruders.colors", "job.nope", "printer.heater_bed.target",
         "printer.box.x"], "b", ["heater_bed"], vmap)
    check("check: known values (and what leads to or lies inside one); a "
          "value another printer has; one nobody has; a Klipper object "
          "this printer lacks",
          missing == ["extruders.colors", "printer.box.x"]
          and unknown == ["job.nope"], (missing, unknown))
    check("values_map: generated, with the three printers",
          "job.file" in values_map.VALUES
          and set(values_map.VALUES["job.file"]) == {
              "creality", "generic", "snapmaker_u1"})
    r = subprocess.run([sys.executable, os.path.join(HERE, "gen_values.py"),
                        "--check"], capture_output=True, text=True)
    check("docs/values.md, values-by-printer.md and values_map.py match "
          "the Objects tree", r.returncode == 0, r.stdout + r.stderr)
    return _failures


if __name__ == "__main__":
    run_values_tests()
    print(f"{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
