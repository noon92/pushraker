# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""python -m pushraker install|uninstall|status: wire the package into a
Moonraker + Klipper installation.

  install     put the component shim into Moonraker's components folder,
              enable [push_notifications] in Moonraker's config, include
              the Klipper macro file, and (once) move settings over from
              an INI section such as the Snapmaker U1 extended firmware's
              extended2.cfg [notifications]
  uninstall   take those out again (the settings and secrets stay unless
              --purge)
  status      say what is in place

Paths default to a standard printer_data layout under the running user's
home (~/moonraker, ~/printer_data); the Snapmaker U1 extended firmware's
harness passes its own.
"""

from __future__ import annotations
import argparse
import os
import shutil
import sys
from typing import List, Optional

from . import __version__
from . import settings as cfgmod

SHIM_NAME = "push_notifications.py"
MOONRAKER_INCLUDE = "pushraker.cfg"
KLIPPER_INCLUDE = "pushraker.cfg"
SHIM = '''# pushraker: Moonraker loads components from this folder; this file hands
# over to the installed package. Written by `python -m pushraker install`.
import sys
for _p in {paths!r}:
    if _p and _p not in sys.path:
        sys.path.append(_p)
from pushraker.component import load_component  # noqa: E402,F401
'''


def home(sub: str) -> str:
    return os.path.join(os.path.expanduser("~"), sub)


def parse(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m pushraker",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["install", "uninstall", "status"])
    p.add_argument("--moonraker-dir", default=home("moonraker"),
                   help="Moonraker's source directory (holds moonraker/components)")
    p.add_argument("--data-dir", default=home("printer_data"),
                   help="printer_data (config/, the secrets file lives here)")
    p.add_argument("--moonraker-conf", default=None,
                   help="moonraker.conf (default: <data-dir>/config/moonraker.conf)")
    p.add_argument("--moonraker-include-dir", default=None,
                   help="a directory moonraker.conf already includes: the "
                        "[push_notifications] section is written there as "
                        "pushraker.cfg instead of into moonraker.conf")
    p.add_argument("--printer-cfg", default=None,
                   help="printer.cfg (default: <data-dir>/config/printer.cfg)")
    p.add_argument("--klipper-include-dir", default=None,
                   help="a directory printer.cfg already includes: the macro "
                        "file is copied there instead of being included from "
                        "printer.cfg")
    p.add_argument("--site-packages", action="append", default=[],
                   help="extra sys.path entries for the shim (the package's "
                        "venv, when Moonraker's Python can't see it)")
    p.add_argument("--name", default="Printer",
                   help="the printer's default name in notifications")
    p.add_argument("--error-index-url", default="",
                   help="the printer maker's error-code index, linked from "
                        "fault notifications")
    p.add_argument("--import-ini", nargs=2, metavar=("FILE", "SECTION"),
                   help="settings to move into pushraker.conf when it doesn't "
                        "exist yet (e.g. extended2.cfg notifications)")
    p.add_argument("--import-secrets", nargs=2, metavar=("FILE", "SECTION"),
                   help="the secrets that go with --import-ini (e.g. "
                        "moonraker.secrets notifications)")
    p.add_argument("--purge", action="store_true",
                   help="uninstall: also remove pushraker.conf and the secrets")
    p.add_argument("--owner", default="",
                   help="user[:group] the written files belong to (default: "
                        "whoever owns the directory they go into)")
    return p.parse_args(argv)


def write(path: str, text: str, mode: int = 0o644,
          owner: str = "") -> bool:
    """Write if different; returns whether it changed. Files take the
    owner of their directory unless --owner says otherwise."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except (OSError, UnicodeDecodeError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    chown(tmp, owner, os.path.dirname(path))
    os.replace(tmp, path)
    return True


def chown(path: str, owner: str, like: str) -> None:
    try:
        if owner:
            import pwd
            import grp
            user, _, group = owner.partition(":")
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid if group else \
                pwd.getpwnam(user).pw_gid
        else:
            st = os.stat(like)
            uid, gid = st.st_uid, st.st_gid
        os.chown(path, uid, gid)
    except (KeyError, OSError, ImportError):
        pass


def remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


SAVE_CONFIG_HEADER = "#*# <---------------------- SAVE_CONFIG"


def read_lines(conf: str) -> List[str]:
    try:
        with open(conf, encoding="utf-8") as f:
            return f.read().splitlines(True)
    except (OSError, UnicodeDecodeError):
        return []


def is_header(line: str, section: str) -> bool:
    s = line.strip()
    return s.startswith("[") and s.endswith("]") and \
        s.lower().replace(" ", "") == f"[{section}]".lower().replace(" ", "")


def ensure_section(conf: str, section: str, text: str, owner: str) -> bool:
    """Add a section to a config file if it has none: before Klipper's
    SAVE_CONFIG block when there is one (Klipper refuses anything after
    that header and drops the saved calibration), else at the end."""
    lines = read_lines(conf)
    if any(is_header(ln, section) for ln in lines):
        return False
    at = next((i for i, ln in enumerate(lines)
               if ln.startswith(SAVE_CONFIG_HEADER)), len(lines))
    block = text if text.endswith("\n") else text + "\n"
    before = "".join(lines[:at])
    if before and not before.endswith("\n"):
        before += "\n"
    if before and not before.endswith("\n\n"):
        before += "\n"
    after = "".join(lines[at:])
    return write(conf, before + block + ("\n" + after if after else ""),
                 owner=owner)


def drop_section(conf: str, section: str, owner: str) -> bool:
    """Remove a section from a config file: an [include ...] is its one
    line; anything else runs to the next header or Klipper's SAVE_CONFIG
    block."""
    lines = read_lines(conf)
    if not lines:
        return False
    out: List[str] = []
    skipping = False
    one_line = section.lower().startswith("include ")
    for ln in lines:
        s = ln.strip()
        if is_header(ln, section):
            if one_line:
                continue
            skipping = True
            continue
        if skipping and ((s.startswith("[") and s.endswith("]"))
                         or ln.startswith(SAVE_CONFIG_HEADER)):
            skipping = False
        if not skipping:
            out.append(ln)
    text = "".join(out)
    if text == "".join(lines):
        return False
    return write(conf, text, owner=owner)


def macro_file() -> str:
    return os.path.join(os.path.dirname(__file__), "klipper", KLIPPER_INCLUDE)


def places(a: argparse.Namespace) -> dict:
    config_dir = os.path.join(a.data_dir, "config")
    return dict(
        shim=os.path.join(a.moonraker_dir, "moonraker", "components",
                          SHIM_NAME),
        moonraker_conf=a.moonraker_conf
        or os.path.join(config_dir, "moonraker.conf"),
        moonraker_include=os.path.join(a.moonraker_include_dir,
                                       MOONRAKER_INCLUDE)
        if a.moonraker_include_dir else "",
        printer_cfg=a.printer_cfg or os.path.join(config_dir, "printer.cfg"),
        klipper_include=os.path.join(a.klipper_include_dir, KLIPPER_INCLUDE)
        if a.klipper_include_dir else os.path.join(config_dir,
                                                   KLIPPER_INCLUDE),
        store=cfgmod.Store(config_dir, a.data_dir, name=a.name))


def section_text(a: argparse.Namespace) -> str:
    lines = ["[push_notifications]",
             "# pushraker (python -m pushraker install); settings are in "
             "pushraker.conf",
             f"name: {a.name}"]
    if a.error_index_url:
        lines.append(f"error_index_url: {a.error_index_url}")
    return "\n".join(lines) + "\n"


def cmd_install(a: argparse.Namespace) -> int:
    p = places(a)
    changed = []
    if p["moonraker_include"] and os.path.realpath(p["moonraker_include"]) \
            == os.path.realpath(p["klipper_include"]):
        print("ERROR: --moonraker-include-dir and the Klipper include "
              "directory are the same; both files are named pushraker.cfg")
        return 1
    if not os.path.isdir(os.path.dirname(p["shim"])):
        print(f"ERROR: no Moonraker components directory at "
              f"{os.path.dirname(p['shim'])} (--moonraker-dir)")
        return 1
    if write(p["shim"], SHIM.format(paths=list(a.site_packages)),
             owner=a.owner):
        changed.append(p["shim"])
    if p["moonraker_include"]:
        if write(p["moonraker_include"], section_text(a), owner=a.owner):
            changed.append(p["moonraker_include"])
    elif ensure_section(p["moonraker_conf"], "push_notifications",
                        section_text(a), a.owner):
        changed.append(p["moonraker_conf"])
    with open(macro_file()) as f:
        macros = f.read()
    if write(p["klipper_include"], macros, owner=a.owner):
        changed.append(p["klipper_include"])
    if not a.klipper_include_dir:
        if ensure_section(p["printer_cfg"], f"include {KLIPPER_INCLUDE}",
                          f"[include {KLIPPER_INCLUDE}]\n", a.owner):
            changed.append(p["printer_cfg"])
    store = p["store"]
    if not store.exists():
        s = None
        if a.import_ini:
            s = cfgmod.import_ini(a.import_ini[0], a.import_ini[1],
                                  *(a.import_secrets or ("", "")),
                                  name=a.name)
            if s["enabled"] or s["server"] or s["secret"]:
                print(f"Moving settings from {a.import_ini[0]} "
                      f"[{a.import_ini[1]}] to {store.settings_path}")
            else:
                s = None
        if s is not None:
            store.save(s)
            chown(store.settings_path, a.owner,
                  os.path.dirname(store.settings_path))
            if os.path.exists(store.secrets_path):
                chown(store.secrets_path, a.owner, a.data_dir)
            changed.append(store.settings_path)
        # Otherwise the component writes it at its first start, with the
        # defaults for the printer it finds
    for path in changed:
        print(f"Wrote {path}")
    if not changed:
        print("Already installed; nothing changed.")
    print(f"pushraker {__version__} is set up. Restart Moonraker and "
          "Klipper to load it, then open the notifications page.")
    return 0


def cmd_uninstall(a: argparse.Namespace) -> int:
    p = places(a)
    removed = []
    for path in (p["shim"], p["moonraker_include"], p["klipper_include"]):
        if path and remove(path):
            removed.append(path)
    if not p["moonraker_include"] and drop_section(
            p["moonraker_conf"], "push_notifications", a.owner):
        removed.append(f"[push_notifications] in {p['moonraker_conf']}")
    if not a.klipper_include_dir and drop_section(
            p["printer_cfg"], f"include {KLIPPER_INCLUDE}", a.owner):
        removed.append(f"[include {KLIPPER_INCLUDE}] in {p['printer_cfg']}")
    if a.purge:
        for path in (p["store"].settings_path, p["store"].secrets_path):
            if remove(path):
                removed.append(path)
    for path in removed:
        print(f"Removed {path}")
    if not removed:
        print("Nothing to remove.")
    print("Restart Moonraker and Klipper to unload pushraker.")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    p = places(a)
    print(f"pushraker {__version__}")
    for what, path in (("shim", p["shim"]),
                       ("Moonraker section", p["moonraker_include"]
                        or p["moonraker_conf"]),
                       ("Klipper macros", p["klipper_include"]),
                       ("settings", p["store"].settings_path),
                       ("secrets", p["store"].secrets_path)):
        print(f"  {what}: {path} "
              f"({'present' if os.path.exists(path) else 'missing'})")
    if p["store"].exists():
        print("  " + cfgmod.summary(p["store"].load()))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    a = parse(sys.argv[1:] if argv is None else argv)
    return {"install": cmd_install, "uninstall": cmd_uninstall,
            "status": cmd_status}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
