#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""install.py (the one-line install over SSH) against a fake /proc: finding
Moonraker in its three layouts, the checks, the wheel. No printer, no
network.

  python3 tests/test_install.py
"""

import ast
import contextlib
import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
import install as inst  # noqa: E402

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


def fake_process(proc, pid, argv, home="/root", cgroup="", cwd="/"):
    d = os.path.join(proc, str(pid))
    os.makedirs(d)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
    with open(os.path.join(d, "environ"), "wb") as f:
        f.write(f"HOME={home}\0PATH=/usr/bin\0".encode())
    with open(os.path.join(d, "cgroup"), "w") as f:
        f.write(cgroup)
    os.symlink(cwd, os.path.join(d, "cwd"))


def run_install_tests():
    print("install.py")
    src = open(os.path.join(ROOT, "install.py"), encoding="utf-8").read()
    try:
        ast.parse(src, "install.py", feature_version=(3, 7))
        ok = True
    except SyntaxError as e:
        ok = e
    check("install.py is Python 3.7 syntax", ok is True, ok)
    check("...and standard library only", not any(
        m in src for m in ("import requests", "import apprise", "import pip")))

    work = tempfile.mkdtemp(prefix="pushraker_inst_")
    proc = os.path.join(work, "proc")
    os.makedirs(proc)
    # The Creality Hi: flat Moonraker in the firmware, procd init script
    fake_process(proc, 3851, [
        "/usr/share/moonraker-env/bin/python",
        "/usr/share/moonraker/moonraker.py", "-c",
        "/usr/share/moonraker/moonraker.conf", "-l",
        "/mnt/UDISK/printer_data/logs/moonraker.log"], home="/mnt/UDISK")
    # Not Moonraker
    fake_process(proc, 4345, ["/usr/share/klippy-env/bin/python",
                              "/usr/share/klipper/klippy/klippy.py", "x"])
    fake_process(proc, 12, ["/usr/bin/python3", "-c", "print(1)"])
    found = inst.moonraker_processes(proc)
    check("finds Moonraker, not Klipper or other Pythons",
          [p for p, _ in found] == [3851], found)
    m = inst.describe(*found[0], proc=proc)
    check("Hi: data path from HOME (no -d), moonraker.conf from -c",
          m["data_path"] == "/mnt/UDISK/printer_data"
          and m["conf"] == "/usr/share/moonraker/moonraker.conf"
          and m["python"] == "/usr/share/moonraker-env/bin/python"
          and m["log"] == "/mnt/UDISK/printer_data/logs/moonraker.log", m)
    check("Hi: components beside moonraker.py (flat layout)",
          inst.components_dir(m) == "/usr/share/moonraker/components")
    init = os.path.join(work, "init.d")
    os.makedirs(init)
    for name in ("klipper", "nginx"):
        open(os.path.join(init, name), "w").close()
    # The Hi's /etc/init.d/moonraker (procd)
    open(os.path.join(init, "moonraker"), "w").write(
        "PROG=/usr/share/moonraker-env/bin/python\n"
        "CONF=/usr/share/moonraker/moonraker.conf\n"
        "procd_set_param command ${PROG} /usr/share/moonraker/moonraker.py "
        "-c ${CONF}\n")
    check("Hi: restarted with its init script (procd)",
          inst.restart_method(m, proc, init) ==
          ("initd", os.path.join(init, "moonraker")))
    other = dict(m, script="/mnt/UDISK/test/moonraker/moonraker.py",
                 conf="/mnt/UDISK/test/printer_data/config/moonraker.conf")
    check("...but not for another Moonraker the script doesn't start "
          "(restarted through its API)",
          inst.restart_method(other, proc, init) == ("api", ""))

    # A Raspberry Pi install: packaged layout, systemd, -d, --configfile=
    proc2 = os.path.join(work, "proc2")
    os.makedirs(proc2)
    fake_process(proc2, 900, [
        "/home/pi/moonraker-env/bin/python",
        "/home/pi/moonraker/moonraker/moonraker.py", "-d",
        "/home/pi/printer_data"], home="/home/pi",
        cgroup="0::/system.slice/moonraker.service\n")
    fake_process(proc2, 901, [
        "/home/pi/moonraker-env/bin/python", "moonraker/server.py",
        "--datapath=/home/pi/printer_2_data",
        "--configfile=/home/pi/printer_2_data/config/moonraker.conf"],
        home="/home/pi", cwd="/home/pi/moonraker")
    found = inst.moonraker_processes(proc2)
    check("two Moonrakers found", [p for p, _ in found] == [900, 901])
    m = inst.describe(*found[0], proc=proc2)
    check("packaged layout: moonraker/components",
          inst.components_dir(m) == "/home/pi/moonraker/moonraker/components"
          and m["conf"] == "/home/pi/printer_data/config/moonraker.conf", m)
    old_which = inst.shutil.which
    inst.shutil.which = lambda name: "/bin/systemctl"
    try:
        check("systemd unit from the process's cgroup",
              inst.restart_method(m, proc2, init) ==
              ("systemd", "moonraker.service"))
    finally:
        inst.shutil.which = old_which
    m = inst.describe(*found[1], proc=proc2)
    check("relative script and --opt=value forms",
          m["script"] == "/home/pi/moonraker/moonraker/server.py"
          and m["data_path"] == "/home/pi/printer_2_data"
          and m["conf"] == "/home/pi/printer_2_data/config/moonraker.conf",
          m)

    class A:
        pid = 0
        install_dir = ""
    try:
        inst.find(A(), proc2)
        check("several Moonrakers: refused without --pid", False)
    except inst.Stop as e:
        check("several Moonrakers: refused without --pid",
              "--pid" in str(e))
    # pip-installed Moonraker: python -m moonraker
    proc3 = os.path.join(work, "proc3")
    os.makedirs(proc3)
    fake_process(proc3, 77, ["/opt/venv/bin/python3", "-m", "moonraker"],
                 home="/home/u")
    found = inst.moonraker_processes(proc3)
    m = inst.describe(*found[0], proc=proc3)
    check("python -m moonraker: components of the installed package",
          inst.components_dir(m, run=lambda py, code:
                              "/opt/venv/lib/python3.11/site-packages/"
                              "moonraker\n")
          == "/opt/venv/lib/python3.11/site-packages/moonraker/components"
          and m["data_path"] == "/home/u/printer_data")

    # The floor, worded exactly
    m = {"python": "/x/python"}
    hi = {"python": [3, 9, 12], "apprise": {"version": "1.2.0"},
          "jinja2": {"version": "3.1.2"}, "requests": {"version": "2.32.3"},
          "PIL": {"version": "8.2.0"}}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        problems = inst.check_python(m, hi)
    check("the Hi's packages pass, no warning", problems == []
          and not out.getvalue(), (problems, out.getvalue()))
    old = dict(hi, python=[3, 6, 9], apprise={"version": "1.1.0"},
               jinja2=None, PIL=None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        problems = inst.check_python(m, old)
    text = "\n".join(problems)
    check("refusals say exactly what is missing",
          "Python 3.6.9" in text and "apprise 1.1.0" in text
          and "1.2 or newer" in text and "no 'jinja2' package" in text
          and len(problems) == 3, problems)
    check("no Pillow: a warning, not a refusal",
          "Pillow" in out.getvalue())

    # The wheel
    meta = {"urls": [
        {"packagetype": "sdist", "filename": "pushraker-1.0.tar.gz",
         "url": "u0", "digests": {"sha256": "a"}},
        {"packagetype": "bdist_wheel",
         "filename": "pushraker-1.0-py3-none-any.whl", "url": "u1",
         "digests": {"sha256": "b"}}]}
    check("PyPI: the pure-Python wheel and its sha256",
          inst.pick_wheel(meta) == ("u1", "b",
                                    "pushraker-1.0-py3-none-any.whl"))
    whl = os.path.join(work, "pushraker-1.0-py3-none-any.whl")
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("pushraker/__init__.py", '__version__ = "1.0"\n')
        for f in ("component.py", "__main__.py", "compat.py"):
            z.writestr(f"pushraker/{f}", "")
        z.writestr("pushraker-1.0.dist-info/METADATA", "Name: pushraker\n")
    lib = os.path.join(work, "pr", "lib")
    os.makedirs(lib)
    open(os.path.join(lib, "stale.txt"), "w").close()
    v = inst.unpack(whl, lib)
    check("unpacked into lib, replacing the older copy whole",
          v == "1.0" and os.path.isfile(os.path.join(
              lib, "pushraker", "component.py"))
          and not os.path.exists(os.path.join(lib, "stale.txt"))
          and not os.path.exists(lib + ".old")
          and not os.path.exists(lib + ".new"))
    bad = os.path.join(work, "bad.whl")
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.py", "")
    try:
        inst.unpack(bad, lib)
        check("a wheel with ../ paths is refused", False)
    except inst.Stop:
        check("a wheel with ../ paths is refused", os.path.isfile(
            os.path.join(lib, "pushraker", "component.py")))
    old = os.path.join(work, "old.whl")
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("pushraker/__init__.py", '__version__ = "0.3.7.1"\n')
        z.writestr("pushraker/component.py", "")
    try:
        inst.unpack(old, lib)
        check("a pushraker without compat.py (too old) is refused", False)
    except inst.Stop as e:
        check("a pushraker without compat.py (too old) is refused",
              "too old" in str(e) and os.path.isfile(
                  os.path.join(lib, "pushraker", "compat.py")))

    # Uninstall without the package: the component file and the section
    comps = os.path.join(work, "components")
    os.makedirs(comps)
    open(os.path.join(comps, "pushraker.py"), "w").write(
        "# pushraker shim\n")
    conf = os.path.join(work, "moonraker.conf")
    open(conf, "w").write("[server]\nport: 7125\n\n[pushraker]\nname: Hi\n"
                          "\n[history]\n")
    with contextlib.redirect_stdout(io.StringIO()):
        inst.remove_by_hand({"components": comps, "conf": conf})
    check("uninstall by hand: component file and section gone, backup kept",
          not os.path.exists(os.path.join(comps, "pushraker.py"))
          and open(conf).read() == "[server]\nport: 7125\n\n[history]\n"
          and "[pushraker]" in open(conf + inst.BACKUP).read(),
          repr(open(conf).read()))
    return _failures


if __name__ == "__main__":
    run_install_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
