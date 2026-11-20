#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""pushraker installer: one command, run on the printer over SSH, for a
printer whose Moonraker came with its firmware (no pip, no curl needed):

  python3 -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/noon92/pushraker/main/install.py').read())"

Options go after it (python3 -c "..." --uninstall), or run the file:
python3 install.py [--uninstall] [--version X] [--wheel FILE] ...

What it does:
  * finds the running Moonraker (its command line in /proc): its Python,
    its components folder (flat or packaged layout), moonraker.conf, the
    data path, how it is restarted;
  * checks what pushraker needs: Python 3.7+, Moonraker's job_state
    component, apprise >= 1.2, Jinja2 >= 3.0, requests (it never adds,
    upgrades or removes a package of Moonraker's Python);
  * downloads the pushraker wheel from PyPI (checked against the sha256
    PyPI gives) and unpacks it into <data path>/../pushraker-install/lib,
    outside
    the firmware (pushraker-install/, marked as install.py's own);
  * has the package write the two things Moonraker needs: one component
    file in Moonraker's components folder and a [pushraker] section in
    moonraker.conf (backed up first as moonraker.conf.bak-pushraker);
  * restarts Moonraker (not while printing), checks it loaded pushraker
    (and takes it out again if not), and prints the settings page's URL.

Run it again to upgrade or to put back what a firmware update removed: the
settings (printer_data/config/pushraker.conf) are kept. --uninstall takes
it all out again except the settings (--purge: those too), also when
Moonraker no longer starts (from the paths recorded at install).

Python 3.7 syntax, standard library only: it runs on the printer's own
python3.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

PACKAGE = "pushraker"
PYPI = "https://pypi.org/pypi/pushraker"
BACKUP = ".bak-pushraker"
# install.py's own folder (code under lib/) and the file that marks it as
# install.py's: only a folder with this file is ever deleted, and it holds
# the paths found at install, for an uninstall while Moonraker is down
DEFAULT_DIR = "pushraker-install"
MARKER = ".pushraker-install.json"
# Where an install folder is looked for when Moonraker isn't running
SEARCH = ("/mnt/UDISK", "/usr/data", "~", "/home/*", "/root")
MIN_PYTHON = (3, 7)
NEEDS = (("apprise", (1, 2)), ("jinja2", (3, 0)), ("requests", None))
OPTIONAL = (("PIL", "Pillow", (8, 2)),)
HEALTHY_WAIT = 60.
PAGE = "/server/pushraker/page"


class Stop(Exception):
    """A reason to stop, already worded for the user."""


class NoMoonraker(Stop):
    """No running Moonraker process."""


def say(msg=""):
    print(msg, flush=True)


def warn(msg):
    print("⚠️  " + msg, flush=True)


# ---- finding Moonraker ------------------------------------------------------

def read_proc(path, binary=False):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data if binary else data.decode("utf-8", "replace")
    except OSError:
        return b"" if binary else ""


def moonraker_processes(proc="/proc"):
    """[(pid, argv)] of the processes that look like Moonraker: a python
    running .../moonraker.py, .../moonraker/server.py or -m moonraker."""
    found = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return found
    for pid in sorted(pids, key=int):
        raw = read_proc(os.path.join(proc, pid, "cmdline"), binary=True)
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if len(argv) < 2 or "python" not in os.path.basename(argv[0]):
            continue
        script = argv[1]
        if script.endswith("moonraker.py") or \
                script.endswith(os.path.join("moonraker", "server.py")) or \
                (script == "-m" and len(argv) > 2
                 and argv[2] == "moonraker"):
            found.append((int(pid), argv))
    return found


def option(argv, short, long):
    """The value of -x V / --long V / --long=V in a command line."""
    for i, a in enumerate(argv):
        if a in (short, long) and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
    return None


def environ(pid, proc="/proc"):
    raw = read_proc(os.path.join(proc, str(pid), "environ"), binary=True)
    out = {}
    for item in raw.split(b"\0"):
        k, _, v = item.decode("utf-8", "replace").partition("=")
        if k:
            out[k] = v
    return out


def describe(pid, argv, proc="/proc"):
    """What a Moonraker process tells about the installation."""
    try:
        cwd = os.readlink(os.path.join(proc, str(pid), "cwd"))
    except OSError:
        cwd = "/"
    python = argv[0]
    if not os.path.isabs(python):
        python = os.path.realpath(os.path.join(proc, str(pid), "exe"))
    env = environ(pid, proc)
    home = env.get("HOME") or os.path.expanduser("~")
    m = {"pid": pid, "python": python, "argv": argv, "home": home}
    if argv[1] == "-m":
        m["script"] = None
    else:
        script = argv[1]
        if not os.path.isabs(script):
            script = os.path.join(cwd, script)
        m["script"] = os.path.normpath(script)

    def path(v):
        v = os.path.expanduser(v.replace("~", home, 1)
                               if v.startswith("~") else v)
        return os.path.normpath(v if os.path.isabs(v)
                                else os.path.join(cwd, v))
    d = option(argv, "-d", "--datapath")
    m["data_path"] = path(d) if d else os.path.join(home, "printer_data")
    c = option(argv, "-c", "--configfile")
    m["conf"] = path(c) if c else os.path.join(m["data_path"], "config",
                                               "moonraker.conf")
    lg = option(argv, "-l", "--logfile")
    m["log"] = path(lg) if lg else os.path.join(m["data_path"], "logs",
                                                "moonraker.log")
    return m


def components_dir(m, run=None):
    """Moonraker's components folder: next to the script it runs (flat:
    /usr/share/moonraker/components beside moonraker.py; packaged:
    moonraker/components beside server.py), or the installed package's."""
    if m["script"]:
        return os.path.join(os.path.dirname(m["script"]), "components")
    out = (run or python_run)(m["python"], "import moonraker, os; "
                              "print(os.path.dirname(moonraker.__file__))")
    return os.path.join(out.strip(), "components")


def python_run(python, code, env=None):
    r = subprocess.run([python, "-c", code], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True,
                       cwd="/", env=env)
    if r.returncode:
        raise Stop(f"{python} failed: {r.stderr.strip()[-400:]}")
    return r.stdout


PROBE = r"""
import json, sys, importlib
out = {"python": list(sys.version_info[:3])}
for name in ("apprise", "jinja2", "requests", "PIL", "pushraker"):
    try:
        mod = importlib.import_module(name)
        out[name] = {"version": str(getattr(mod, "__version__", "?")),
                     "file": getattr(mod, "__file__", "") or ""}
    except Exception as e:
        out[name] = None
print(json.dumps(out))
"""


def version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3]) or (0,)


def check_python(m, probe):
    """Refuse below the floor, saying exactly what is missing."""
    problems = []
    py = tuple(probe["python"])
    if py[:2] < MIN_PYTHON:
        problems.append(
            f"Moonraker runs Python {'.'.join(map(str, py))} "
            f"({m['python']}); pushraker needs Python "
            f"{'.'.join(map(str, MIN_PYTHON))} or newer.")
    for name, least in NEEDS:
        got = probe.get(name)
        if got is None:
            problems.append(
                f"Moonraker's Python ({m['python']}) has no '{name}' "
                f"package; pushraker needs it"
                + (f" ({'.'.join(map(str, least))} or newer)" if least
                   else "")
                + ". install.py never adds packages to Moonraker's "
                  "Python.")
        elif least and version_tuple(got["version"]) < least:
            problems.append(
                f"Moonraker's Python has {name} {got['version']}; "
                f"pushraker needs {'.'.join(map(str, least))} or newer. "
                f"install.py doesn't upgrade it.")
    for name, label, least in OPTIONAL:
        got = probe.get(name)
        if got is None:
            warn(f"Moonraker's Python has no {label}: notifications go "
                 f"without spool pictures and preview images.")
        elif version_tuple(got["version"]) < least:
            warn(f"Moonraker's Python has {label} {got['version']}; "
                 f"pushraker is checked down to "
                 f"{'.'.join(map(str, least))}.")
    return problems


# ---- Moonraker's API ----------------------------------------------------------

def moonraker_port(conf):
    p = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        p.read(conf, encoding="utf-8")
        return p.getint("server", "port", fallback=7125)
    except (configparser.Error, ValueError, UnicodeDecodeError):
        return 7125


def api(port, path, method="GET", timeout=10.):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("result")


def printing_state(port):
    """print_stats.state, or "" when Klipper isn't ready (then it isn't
    printing either). Raises Stop when Moonraker doesn't answer."""
    try:
        info = api(port, "/server/info")
    except (OSError, ValueError) as e:
        raise Stop(f"Moonraker doesn't answer on port {port}: {e}")
    if info.get("klippy_state") != "ready":
        return ""
    try:
        st = api(port, "/printer/objects/query?print_stats=state")
        return st["status"]["print_stats"]["state"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Stop(f"Couldn't read the printer's state: {e}")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent: this only picks the interface of the route
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


# ---- restarting --------------------------------------------------------------

def restart_method(m, proc="/proc", init_dir="/etc/init.d"):
    """("systemd", unit) | ("initd", script) | ("api", "")."""
    cg = read_proc(os.path.join(proc, str(m["pid"]), "cgroup"))
    unit = re.search(r"/([^/\s]+\.service)\b", cg)
    if unit and shutil.which("systemctl"):
        return "systemd", unit.group(1)
    try:
        names = sorted(os.listdir(init_dir))
    except OSError:
        names = []
    # The init script that starts this Moonraker: it names its script or
    # its config (the Creality Hi's /etc/init.d/moonraker names both). A
    # script for another Moonraker would restart the wrong one.
    marks = [x for x in (m.get("script"), m.get("conf")) if x]
    for name in names:
        if "moonraker" not in name.lower():
            continue
        path = os.path.join(init_dir, name)
        text = read_proc(path)
        if any(x in text for x in marks):
            return "initd", path
    return "api", ""


def restart(m, port):
    how, what = m["restart"]
    if how == "systemd":
        cmd = ["systemctl", "restart", what]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        say(f"Restarting Moonraker: {' '.join(cmd)}")
        r = subprocess.run(cmd)
        if r.returncode:
            raise Stop(f"'{' '.join(cmd)}' failed; restart Moonraker "
                       f"yourself and run this again to check.")
    elif how == "initd":
        say(f"Restarting Moonraker: {what} restart")
        r = subprocess.run([what, "restart"])
        if r.returncode:
            raise Stop(f"'{what} restart' failed (exit {r.returncode}).")
    else:
        say("Restarting Moonraker through its API (/server/restart)")
        try:
            api(port, "/server/restart", "POST")
        except (OSError, ValueError):
            pass    # it may close the connection while restarting
        warn("Moonraker restarted itself in the same process. On an "
             "upgrade, restart its service (or the printer) so the new "
             "code is loaded.")


def klippy_connected(port):
    """Whether Moonraker is connected to Klipper now (False if it doesn't
    answer): a restart is only expected to bring back what was there."""
    try:
        return bool((api(port, "/server/info", timeout=5.) or {})
                    .get("klippy_connected"))
    except (OSError, ValueError):
        return False


def wait_healthy(port, want_loaded, need_klippy=True):
    """Wait for Moonraker to answer again, with pushraker loaded (or not),
    and connected to Klipper if it was before. Returns (ok, info)."""
    deadline = time.monotonic() + HEALTHY_WAIT
    info = None
    time.sleep(3.)
    while time.monotonic() < deadline:
        try:
            info = api(port, "/server/info", timeout=5.)
        except (OSError, ValueError):
            info = None
        if info:
            comps = info.get("components") or []
            failed = info.get("failed_components") or []
            if want_loaded and PACKAGE in failed:
                return False, info
            if (PACKAGE in comps) == want_loaded and \
                    (info.get("klippy_connected") or not need_klippy):
                return True, info
        time.sleep(2.)
    return False, info


def log_lines(path, words=("pushraker", "Traceback", "Error"), n=25):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-3000:]
    except OSError:
        return []
    return [ln.rstrip() for ln in lines if any(w in ln for w in words)][-n:]


# ---- the wheel ------------------------------------------------------------------

def pick_wheel(meta):
    """(url, sha256, filename) of the pure-Python wheel in PyPI's JSON."""
    for f in meta.get("urls") or []:
        if f.get("packagetype") == "bdist_wheel" and \
                f.get("filename", "").endswith("-py3-none-any.whl"):
            return f["url"], (f.get("digests") or {}).get("sha256", ""), \
                f["filename"]
    raise Stop("PyPI lists no pure-Python wheel for this pushraker version.")


def fetch_wheel(version, workdir):
    url = f"{PYPI}/{version}/json" if version else f"{PYPI}/json"
    say(f"Looking up pushraker {version or '(latest)'} on PyPI")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            meta = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise Stop(f"PyPI: {e.code} for {url}"
                   + (" (no such version?)" if e.code == 404 else ""))
    except OSError as e:
        raise Stop(f"Can't reach PyPI ({e}). Download the wheel elsewhere "
                   f"and use --wheel FILE.")
    wurl, sha, name = pick_wheel(meta)
    if not sha:
        raise Stop("PyPI gave no sha256 for the wheel; not installing it.")
    path = os.path.join(workdir, name)
    say(f"Downloading {name}")
    with urllib.request.urlopen(wurl, timeout=120) as r, \
            open(path, "wb") as f:
        shutil.copyfileobj(r, f)
    got = sha256(path)
    if got != sha:
        raise Stop(f"The download's sha256 is {got}, PyPI says {sha}: "
                   f"not installing it.")
    say(f"sha256 checked: {sha}")
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(wheel, lib):
    """The wheel's files into lib (replacing an older copy only once the
    new one is complete)."""
    new = lib + ".new"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            target = os.path.normpath(os.path.join(new, name))
            if not target.startswith(new + os.sep):
                raise Stop(f"The wheel has an unsafe path: {name}")
        z.extractall(new)
    for need in ("component.py", "__main__.py", "compat.py"):
        if not os.path.isfile(os.path.join(new, PACKAGE, need)):
            shutil.rmtree(new, ignore_errors=True)
            raise Stop(f"The wheel has no {PACKAGE}/{need}: too old for "
                       f"install.py (pushraker with older-Moonraker "
                       f"support is needed).")
    old = lib + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(lib):
        os.rename(lib, old)
    os.rename(new, lib)
    shutil.rmtree(old, ignore_errors=True)
    with open(os.path.join(lib, PACKAGE, "__init__.py"),
              encoding="utf-8") as f:
        v = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
    return v.group(1) if v else "?"


# ---- the package's own installer ---------------------------------------------------

def cli_args(m, a):
    args = ["--moonraker-dir", os.path.dirname(m["components"]),
            "--components-dir", m["components"],
            "--data-dir", m["data_path"],
            "--moonraker-conf", m["conf"],
            "--site-packages", m["lib"],
            "--backup", BACKUP]
    if a.name:
        args += ["--name", a.name]
    if not a.klipper:
        args.append("--no-klipper")
    return args


def run_cli(m, command, args):
    env = dict(os.environ, PYTHONPATH=m["lib"], PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([m["python"], "-m", PACKAGE, command] + args,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, cwd="/", env=env)
    for line in r.stdout.splitlines():
        say("  " + line)
    if r.returncode:
        raise Stop(f"python -m pushraker {command} failed "
                   f"(exit {r.returncode}).")


def remove_by_hand(m):
    """Uninstall without the package (its folder already gone): the
    component file, and the [pushraker] section of moonraker.conf."""
    shim = os.path.join(m["components"], "pushraker.py")
    if os.path.isfile(shim):
        with open(shim, encoding="utf-8", errors="replace") as f:
            ours = "pushraker" in f.read()
        if ours:
            os.remove(shim)
            say(f"  Removed {shim}")
    try:
        with open(m["conf"], encoding="utf-8") as f:
            lines = f.read().splitlines(True)
    except OSError:
        return
    out, skip = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            skip = s.replace(" ", "").lower() == "[pushraker]"
            if skip:
                continue
        if not skip:
            out.append(ln)
    if out != lines:
        if not os.path.exists(m["conf"] + BACKUP):
            shutil.copy2(m["conf"], m["conf"] + BACKUP)
        with open(m["conf"] + ".tmp", "w", encoding="utf-8") as f:
            f.write("".join(out))
        os.replace(m["conf"] + ".tmp", m["conf"])
        say(f"  Removed [pushraker] from {m['conf']}")


# ---- main ------------------------------------------------------------------------

def parse(argv):
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Install pushraker into the running Moonraker.")
    p.add_argument("--uninstall", action="store_true",
                   help="take pushraker out again (settings are kept)")
    p.add_argument("--purge", action="store_true",
                   help="with --uninstall: remove the settings and secrets "
                        "too")
    p.add_argument("--version", default="",
                   help="the pushraker version from PyPI (default: latest)")
    p.add_argument("--wheel", default="",
                   help="install this wheel file instead of downloading one")
    p.add_argument("--name", default="",
                   help="the printer's default name in notifications")
    p.add_argument("--install-dir", default="",
                   help="where pushraker's code goes (default: "
                        "pushraker-install/ next to Moonraker's data folder; "
                        "an existing folder must be empty or install.py's)")
    p.add_argument("--klipper", action="store_true",
                   help="also add pushraker's Klipper macro file and an "
                        "[include] for it to printer.cfg (for the print-end "
                        "snapshot hooks; Klipper needs a restart)")
    p.add_argument("--no-restart", action="store_true",
                   help="change the files only; restart Moonraker yourself")
    p.add_argument("--pid", type=int, default=0,
                   help="the Moonraker process to use when there are several")
    return p.parse_args(argv)


def find(a, proc="/proc"):
    procs = moonraker_processes(proc)
    if a.pid:
        procs = [(p, argv) for p, argv in procs if p == a.pid]
    if not procs:
        raise NoMoonraker("No running Moonraker found (no python process running "
                   "moonraker.py, moonraker/server.py or -m moonraker). "
                   "Start Moonraker and run this again.")
    if len(procs) > 1:
        raise Stop("Several Moonrakers are running; choose one with --pid:\n"
                   + "\n".join(f"  {p}: {' '.join(argv)}"
                               for p, argv in procs))
    m = describe(*procs[0], proc=proc)
    m["components"] = components_dir(m)
    m["restart"] = restart_method(m, proc)
    install_dir = os.path.abspath(os.path.expanduser(
        a.install_dir or os.path.join(os.path.dirname(m["data_path"]),
                                      DEFAULT_DIR)))
    m["install_dir"] = install_dir
    m["lib"] = os.path.join(install_dir, "lib")
    m["port"] = moonraker_port(m["conf"])
    return m


def show(m):
    say(f"Moonraker: {'pid ' + str(m['pid']) if m.get('pid') else 'not running'}"
        f", {m['python']}")
    say(f"  components: {m['components']}")
    say(f"  config:     {m['conf']}")
    say(f"  data:       {m['data_path']}")
    say(f"  restart:    {m['restart'][0]} {m['restart'][1]}".rstrip())
    say(f"  pushraker:  {m['lib']}")


def check_floor(m):
    problems = []
    if not os.path.isdir(m["components"]):
        problems.append(f"Moonraker's components folder isn't at "
                        f"{m['components']}.")
    elif not os.path.isfile(os.path.join(m["components"], "job_state.py")):
        problems.append(
            f"This Moonraker has no job_state component "
            f"({m['components']}/job_state.py; upstream Moonraker has had "
            f"it since November 2021). pushraker needs its print job "
            f"events.")
    if not os.path.isfile(m["conf"]):
        problems.append(f"moonraker.conf isn't at {m['conf']}.")
    elif not os.access(m["conf"], os.W_OK):
        problems.append(f"{m['conf']} isn't writable by this user.")
    if os.path.isdir(m["components"]) and \
            not os.access(m["components"], os.W_OK):
        problems.append(f"{m['components']} isn't writable by this user.")
    probe = json.loads(python_run(m["python"], PROBE))
    problems += check_python(m, probe)
    own = probe.get(PACKAGE)
    if own and not os.path.realpath(own["file"]).startswith(
            os.path.realpath(m["lib"]) + os.sep):
        problems.append(
            f"pushraker {own['version']} is already installed in "
            f"Moonraker's Python ({own['file']}); it would be used instead "
            f"of this one. Update that one with pip and "
            f"'python -m pushraker install', or remove it first.")
    if problems:
        raise Stop("Can't install pushraker here:\n" + "\n".join(
            "  - " + p for p in problems)
            + "\nIf this is a printer pushraker should support, report "
              "these lines at https://github.com/noon92/pushraker/issues")
    return probe


def marker(install_dir):
    return os.path.join(install_dir, MARKER)


def claim_dir(install_dir):
    """The install folder is new, empty or install.py's own; anything
    else (a clone of the repo, a home folder) is left alone."""
    if not os.path.isdir(install_dir):
        if os.path.exists(install_dir):
            raise Stop(f"{install_dir} exists and isn't a folder; choose "
                       f"another --install-dir.")
        return
    if os.listdir(install_dir) and not os.path.isfile(marker(install_dir)):
        raise Stop(f"{install_dir} already exists and wasn't made by "
                   f"install.py (no {MARKER} in it); choose another "
                   f"--install-dir, or empty it.")


RECORDED = ("python", "components", "conf", "data_path", "log", "port",
            "restart", "install_dir", "lib")


def write_marker(m, version):
    data = {k: m[k] for k in RECORDED}
    data["version"] = version
    data["installed"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(marker(m["install_dir"]) + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(marker(m["install_dir"]) + ".tmp", marker(m["install_dir"]))


def read_marker(install_dir):
    try:
        with open(marker(install_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not all(k in data for k in RECORDED):
        return None
    data["restart"] = tuple(data["restart"])
    data["pid"] = 0
    data["script"] = None
    return data


def recorded_installs(a):
    """The install folders install.py made, from --install-dir or the
    usual places."""
    import glob
    if a.install_dir:
        dirs = [os.path.abspath(os.path.expanduser(a.install_dir))]
    else:
        dirs = []
        for base in SEARCH:
            for d in glob.glob(os.path.join(os.path.expanduser(base),
                                            DEFAULT_DIR)):
                dirs.append(os.path.abspath(d))
    out = []
    for d in dict.fromkeys(dirs):
        m = read_marker(d)
        if m is not None:
            out.append(m)
    return out


def ensure_idle(m, a):
    if a.no_restart:
        return
    state = printing_state(m["port"])
    if state in ("printing", "paused"):
        raise Stop(f"The printer is {state}: Moonraker can't be restarted "
                   f"now. Run this again when the print is over (or use "
                   f"--no-restart and restart Moonraker later).")


def install(a, m):
    probe = check_floor(m)
    ensure_idle(m, a)
    claim_dir(m["install_dir"])
    os.makedirs(m["install_dir"], exist_ok=True)
    if not os.path.isfile(marker(m["install_dir"])):
        # Marked at once: a download or unpack that fails leaves a folder
        # the next run recognises as its own
        write_marker(m, "?")
    work = tempfile.mkdtemp(prefix="pushraker_", dir=m["install_dir"])
    try:
        if a.wheel:
            wheel = os.path.abspath(a.wheel)
            say(f"Using {wheel} (sha256 {sha256(wheel)})")
        else:
            wheel = fetch_wheel(a.version, work)
        version = unpack(wheel, m["lib"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_marker(m, version)
    say(f"pushraker {version} unpacked into {m['lib']}")
    run_cli(m, "install", cli_args(m, a))
    if a.no_restart:
        say("Restart Moonraker to load pushraker.")
        return 0
    # Klipper down or in error before: not a reason to undo the install
    need_klippy = klippy_connected(m["port"])
    restart(m, m["port"])
    ok, info = wait_healthy(m["port"], True, need_klippy)
    if not ok:
        warn("Moonraker didn't come back with pushraker loaded within "
             f"{HEALTHY_WAIT:.0f} s. Taking pushraker out again.")
        for line in log_lines(m["log"]):
            say("  " + line)
        run_cli(m, "uninstall", cli_args(m, a))
        restart(m, m["port"])
        back, _ = wait_healthy(m["port"], False, need_klippy)
        raise Stop("pushraker was removed again; Moonraker is "
                   + ("running." if back else
                      "NOT answering: check it (" + m["log"] + ")."))
    say("")
    say(f"pushraker {version} is running in Moonraker "
        f"(Python {'.'.join(map(str, probe['python']))}).")
    say(f"Settings page (served by Moonraker): "
        f"http://{lan_ip()}:{m['port']}{PAGE}")
    say("  (Through the printer's own web interface only where its web "
        "server is set up for it: see the README.)")
    say(f"Settings file: {os.path.join(m['data_path'], 'config', 'pushraker.conf')}")
    say("Run this command again to update, or after a firmware update "
        "removed it; the settings are kept.")
    return 0


def installed_in(m):
    """Whether this Moonraker has pushraker's component file or section."""
    if os.path.isfile(os.path.join(m["components"], "pushraker.py")):
        return True
    try:
        with open(m["conf"], encoding="utf-8", errors="replace") as f:
            return any(ln.strip().replace(" ", "").lower() == "[pushraker]"
                       for ln in f)
    except OSError:
        return False


def take_out(a, m):
    args = cli_args(m, a)
    if a.purge:
        args.append("--purge")
    try:
        run_cli(m, "uninstall", args)
    except Stop:
        remove_by_hand(m)
    if not os.path.isfile(os.path.join(m["lib"], PACKAGE, "__main__.py")):
        remove_by_hand(m)


def remove_install_dir(m):
    if os.path.isfile(marker(m["install_dir"])):
        shutil.rmtree(m["install_dir"], ignore_errors=True)
        say(f"Removed {m['install_dir']}")
    elif os.path.isdir(m["install_dir"]):
        say(f"Left {m['install_dir']} alone: it isn't marked as "
            f"install.py's ({MARKER}).")


def uninstall(a, m):
    if not installed_in(m):
        say(f"pushraker isn't in this Moonraker ({m['conf']}): nothing to "
            f"take out, nothing restarted.")
        remove_install_dir(m)
        return 0
    ensure_idle(m, a)
    need_klippy = klippy_connected(m["port"])
    take_out(a, m)
    if not a.no_restart:
        restart(m, m["port"])
        ok, _ = wait_healthy(m["port"], False, need_klippy)
        if not ok:
            raise Stop("Moonraker didn't come back healthy without "
                       f"pushraker; check it ({m['log']}). pushraker's "
                       f"code is left at {m['install_dir']}.")
    remove_install_dir(m)
    say("pushraker is uninstalled"
        + ("." if a.purge else "; its settings are kept (--purge removes "
           "them)."))
    return 0


def uninstall_offline(a, why):
    """--uninstall with no Moonraker running (the case that matters most:
    it doesn't start any more): the paths recorded at install, then start
    Moonraker. There is no printing check (it would go through Moonraker);
    starting Moonraker doesn't touch a print Klipper is running."""
    found = recorded_installs(a)
    if not found:
        raise Stop(f"{why}\nNo install made by install.py found either "
                   f"(looked for {DEFAULT_DIR}/{MARKER} in "
                   f"{', '.join(SEARCH)}); give --install-dir.")
    if len(found) > 1:
        raise Stop("Several installs found; choose one with --install-dir:\n"
                   + "\n".join("  " + m["install_dir"] for m in found))
    m = found[0]
    say("That Moonraker isn't running; uninstalling from what install.py "
        f"recorded in {marker(m['install_dir'])}")
    show(m)
    take_out(a, m)
    how, what = m["restart"]
    if a.no_restart or how == "api":
        remove_install_dir(m)
        say("Start Moonraker yourself now"
            + ("." if a.no_restart else " (it has no service install.py "
               "knows how to start)."))
        return 0
    restart(m, m["port"])
    ok, _ = wait_healthy(m["port"], False, False)
    remove_install_dir(m)
    if not ok:
        raise Stop(f"pushraker is out, but Moonraker didn't answer within "
                   f"{HEALTHY_WAIT:.0f} s: check its log ({m['log']}).")
    say("Moonraker is running again, without pushraker.")
    return 0


def choose_uninstall(a, proc):
    """The Moonraker to take pushraker out of: the running one whose
    config is the one recorded at install; else, with an install recorded
    (and --install-dir naming it, or none running), from the record with
    Moonraker down. Never another Moonraker that merely happens to run."""
    recorded = recorded_installs(a)
    try:
        live = find(a, proc)
    except NoMoonraker as e:
        return uninstall_offline(a, str(e))
    for rec in recorded:
        if os.path.realpath(rec["conf"]) == os.path.realpath(live["conf"]):
            live.update(install_dir=rec["install_dir"], lib=rec["lib"])
            show(live)
            return uninstall(a, live)
    if a.install_dir and recorded:
        return uninstall_offline(a, "The running Moonraker isn't the one "
                                 "recorded in " + recorded[0]["install_dir"])
    show(live)
    return uninstall(a, live)


def main(argv=None, proc="/proc"):
    a = parse(sys.argv[1:] if argv is None else argv)
    try:
        if sys.version_info[:2] < MIN_PYTHON:
            raise Stop("install.py needs Python 3.7 or newer to run.")
        if a.uninstall:
            return choose_uninstall(a, proc)
        m = find(a, proc)
        show(m)
        return install(a, m)
    except Stop as e:
        say("")
        warn(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
