# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Promises the Bespok3d package makes that only fail after an install."""

import json
import os
import re
import sys

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
REPO = os.path.join(PLUGIN, "..")
BAKED = os.path.join("files", "site-packages")


def manifest():
    with open(os.path.join(PLUGIN, "manifest.json")) as f:
        return json.load(f)


def read(*parts):
    with open(os.path.join(*parts)) as f:
        return f.read()


def test_every_placed_file_exists():
    for entry in manifest()["install"]["place"]:
        assert os.path.isfile(os.path.join(PLUGIN, entry["src"])), entry["src"]


def test_the_macro_file_is_the_package_s_own():
    assert read(PLUGIN, "files", "cfg", "klipper", "pushraker.cfg") == read(
        REPO, "src", "pushraker", "klipper", "pushraker.cfg")


def test_permissions_cover_every_class_and_restart():
    data = manifest()
    classes = {entry["class"] for entry in data["install"]["place"]}
    assert classes <= set(data["permissions"])
    assert "restart" in data["permissions"]
    assert set(data["install"]["restart"]) == {"klipper", "moonraker"}


def test_every_template_variable_is_declared_and_asked():
    data = manifest()
    keys = {item["key"] for item in data["config"]}
    variables = {item["name"] for item in data["requires"]["variables"]}
    for entry in data["install"]["place"]:
        if not entry.get("render"):
            continue
        assert entry["src"].endswith(".tmpl")
        used = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)",
                              read(PLUGIN, entry["src"])))
        assert used, entry["src"]
        assert used <= keys and used <= variables, used - keys


def test_versions_agree():
    data = manifest()
    # A test build is <pushraker version>-test<n>
    version = data["version"].split("-")[0]
    pin = read(PLUGIN, "klipper_requirements.txt").split()
    assert pin == ["pushraker==" + version]
    assert data["sw_version"] == "pushraker " + version
    pyproject = read(REPO, "pyproject.toml")
    assert '\nversion = "%s"' % version in pyproject
    assert "## " + version in read(PLUGIN, "doc", "CHANGELOG.md")


def test_component_and_extra_names():
    data = manifest()
    by_class = {}
    for entry in data["install"]["place"]:
        name = entry.get("name") or os.path.basename(entry["src"])
        by_class.setdefault(entry["class"], []).append(name)
    # Moonraker loads a component by its file name, the [pushraker] section
    assert by_class["moonraker-component"] == ["pushraker.py"]
    assert "[pushraker]" in read(PLUGIN, "files", "cfg", "moonraker",
                                 "pushraker.cfg.tmpl")
    # Klipper loads an extra by its file name, from its config section
    assert by_class["klipper-extra"] == ["pushraker_print_hooks.py"]
    assert "[pushraker_print_hooks]" in read(
        PLUGIN, "files", "cfg", "klipper", "pushraker-print-hooks.cfg")
    names = by_class["klipper-config"]
    assert len(names) == len(set(names))


def test_the_hook_macros_match_the_prefixes():
    macros = read(REPO, "src", "pushraker", "klipper", "pushraker.cfg")
    extra = read(PLUGIN, "files", "klipper", "pushraker_print_hooks.py")
    for macro, prefix in (("_PRINT_END_NTFY", "_PRINT_END_"),
                          ("_CANCEL_PRINT_NTFY", "_CANCEL_PRINT_")):
        assert "[gcode_macro %s]" % macro in macros
        assert macro.startswith(prefix) and '"%s"' % prefix in extra


def test_attributions_file_matches_the_manifest():
    assert read(PLUGIN, "doc", "ATTRIBUTIONS.md") == manifest()["attributions"]


def test_nothing_stray_ships():
    for root, dirs, files in os.walk(os.path.join(PLUGIN, "files")):
        rel = os.path.relpath(root, PLUGIN)
        if rel.startswith(BAKED):
            continue
        assert "__pycache__" not in dirs, rel
        for name in files:
            assert not name.endswith((".pyc", ".DS_Store")), name
            mode = os.stat(os.path.join(root, name)).st_mode & 0o777
            assert mode in (0o644, 0o755), (name, oct(mode))


def test_source_and_publisher():
    data = manifest()
    assert data["publisher"] == "PLACEHOLDER"
    assert data["source"] == "https://github.com/noon92/pushraker"
    assert data["files"] == []


if __name__ == "__main__":
    failed = 0
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            try:
                test()
                print("  ok", name)
            except AssertionError as e:
                failed += 1
                print("FAIL", name, e)
    print("%d failed" % failed)
    sys.exit(1 if failed else 0)
