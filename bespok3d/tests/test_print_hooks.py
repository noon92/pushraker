# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The print hooks Klipper extra, against a stand-in for Klipper's gcode
dispatcher that behaves like the real one: register_command(name, None)
removes the command and returns its handler; registering a name that
exists is an error."""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRA = os.path.join(HERE, "..", "files", "klipper", "pushraker_print_hooks.py")


def load_extra():
    spec = importlib.util.spec_from_file_location("pushraker_print_hooks",
                                                  EXTRA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GCode:
    def __init__(self, log):
        self.log = log
        self.handlers = {}
        self.gcode_help = {}
        self.failing = set()

    def register_command(self, name, handler, desc=None):
        if handler is None:
            return self.handlers.pop(name, None)
        if name in self.handlers:
            raise RuntimeError("gcode command %s already registered" % name)
        self.handlers[name] = handler
        if desc is not None:
            self.gcode_help[name] = desc

    def run_script_from_command(self, script):
        self.log.append(script)
        if script in self.failing:
            raise RuntimeError("boom")

    def respond_info(self, text):
        self.log.append("info: " + text)

    def run(self, name):
        self.handlers[name]("gcmd")


class ConfigFile:
    def __init__(self, sections):
        self.sections = sections

    def get_status(self, eventtime):
        return {"config": self.sections}


class Printer:
    def __init__(self, gcode, macros, sections):
        self.gcode = gcode
        self.macros = macros
        self.configfile = ConfigFile(sections)
        self.handlers = {}

    def lookup_object(self, name, default=None):
        return {"gcode": self.gcode, "configfile": self.configfile}.get(
            name, default)

    def lookup_objects(self, module):
        return [("gcode_macro " + m, object()) for m in self.macros]

    def register_event_handler(self, event, callback):
        self.handlers[event] = callback


class Config:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer


def stock_printer(macros, sections=None):
    log = []
    gcode = GCode(log)
    gcode.register_command("PRINT_END", lambda g: log.append("stock end"),
                           desc="stock print end")
    gcode.register_command("CANCEL_PRINT",
                           lambda g: log.append("stock cancel"))
    printer = Printer(gcode, macros, sections or {})
    hooks = load_extra().load_config(Config(printer))
    printer.handlers["klippy:ready"]()
    return log, gcode, hooks


def test_hooks_run_before_the_stock_macros_in_config_order():
    log, gcode, hooks = stock_printer(
        ["_NTFY_HOOKS", "_PRINT_END_NTFY", "_CANCEL_PRINT_NTFY",
         "_PRINT_END_OTHER", "PRINT_START"])
    gcode.run("PRINT_END")
    assert log == ["_PRINT_END_NTFY", "_PRINT_END_OTHER", "stock end"]
    log.clear()
    gcode.run("CANCEL_PRINT")
    assert log == ["_CANCEL_PRINT_NTFY", "stock cancel"]
    assert hooks.taken_over == ["PRINT_END", "CANCEL_PRINT"]
    assert gcode.gcode_help["PRINT_END"] == "stock print end"


def test_a_failing_hook_never_stops_the_stock_macro():
    log, gcode, _ = stock_printer(["_PRINT_END_NTFY", "_PRINT_END_OTHER"])
    gcode.failing.add("_PRINT_END_NTFY")
    gcode.run("PRINT_END")
    assert log[0] == "_PRINT_END_NTFY"
    assert log[1].startswith("info: pushraker print hooks: _PRINT_END_NTFY "
                             "failed")
    assert log[2:] == ["_PRINT_END_OTHER", "stock end"]


def test_lowercase_macro_names_count():
    log, gcode, _ = stock_printer(["_print_end_ntfy"])
    gcode.run("PRINT_END")
    assert log == ["_print_end_ntfy", "stock end"]


def test_left_alone_when_the_firmware_already_calls_the_hooks():
    # The Snapmaker U1 extended firmware's PRINT_END loops over the hooks
    extended = {"gcode_macro PRINT_END": {"gcode": (
        '{% for section in printer.configfile.config if '
        'section.startswith("gcode_macro _PRINT_END_") %}')}}
    log, gcode, hooks = stock_printer(["_PRINT_END_NTFY"], extended)
    gcode.run("PRINT_END")
    assert log == ["stock end"]
    assert hooks.taken_over == ["CANCEL_PRINT"]


def test_a_printer_without_the_command_is_left_as_it_is():
    log = []
    gcode = GCode(log)
    printer = Printer(gcode, ["_PRINT_END_NTFY"], {})
    hooks = load_extra().load_config(Config(printer))
    printer.handlers["klippy:ready"]()
    assert hooks.taken_over == []
    assert gcode.handlers == {}


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
