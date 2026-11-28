# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Print hooks for printers on their stock firmware.

When Klipper is ready, this takes over PRINT_END and CANCEL_PRINT: each
first runs every macro named _PRINT_END_<anything> (or
_CANCEL_PRINT_<anything>), in config order, then the printer's own
PRINT_END (CANCEL_PRINT), unchanged. The same convention as the Snapmaker
U1 extended firmware's hook system, which edits the stock macros instead;
pushraker's _PRINT_END_NTFY and _CANCEL_PRINT_NTFY lift the toolhead and
report the event there, so the snapshot is taken before the printer parks.

No file the printer maker ships is changed: the original commands are
taken over in memory, the way Klipper's own rename_existing does. A hook
that fails is logged and skipped, so the printer's own print-end routine
(heaters and motors off) always runs.

Klipper config:

    [pushraker_print_hooks]
"""

import logging

# Command, then the prefix of the macros run before it
HOOKED = (("PRINT_END", "_PRINT_END_"), ("CANCEL_PRINT", "_CANCEL_PRINT_"))
MACRO = "gcode_macro "


class PushrakerPrintHooks:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.taken_over = []
        # At ready, every macro's own rename_existing (done at connect)
        # has happened: what we take over is the final command
        self.printer.register_event_handler("klippy:ready", self.take_over)

    def take_over(self):
        for command, prefix in HOOKED:
            self.take_over_command(command, prefix)

    def take_over_command(self, command, prefix):
        if self.already_hooked(command, prefix):
            logging.info("pushraker_print_hooks: %s already runs %s* "
                         "macros; left alone", command, prefix)
            return
        original = self.gcode.register_command(command, None)
        if original is None:
            logging.info("pushraker_print_hooks: no %s on this printer",
                         command)
            return
        help_text = self.gcode_help(command)

        def with_hooks(gcmd):
            self.run_hooks(prefix)
            original(gcmd)
        self.gcode.register_command(command, with_hooks, desc=help_text)
        self.taken_over.append(command)
        logging.info("pushraker_print_hooks: %s runs %s* macros first",
                     command, prefix)

    def gcode_help(self, command):
        return getattr(self.gcode, "gcode_help", {}).get(command)

    def already_hooked(self, command, prefix):
        """A firmware that already calls the hooks from its own macro (the
        Snapmaker U1 extended firmware) would run each one twice."""
        configfile = self.printer.lookup_object("configfile", None)
        if configfile is None:
            return False
        sections = configfile.get_status(0.).get("config", {})
        template = (sections.get(MACRO + command) or {}).get("gcode", "")
        return (MACRO + prefix) in template

    def hook_names(self, prefix):
        names = []
        for section, _macro in self.printer.lookup_objects("gcode_macro"):
            name = section[len(MACRO):]
            if name.upper().startswith(prefix):
                names.append(name)
        return names

    def run_hooks(self, prefix):
        for name in self.hook_names(prefix):
            try:
                self.gcode.run_script_from_command(name)
            except Exception as e:
                logging.exception("pushraker_print_hooks: %s failed", name)
                self.gcode.respond_info(
                    "pushraker print hooks: %s failed (%s); carrying on"
                    % (name, e))

    def get_status(self, eventtime):
        return {"taken_over": list(self.taken_over)}


def load_config(config):
    return PushrakerPrintHooks(config)
