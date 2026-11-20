# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""What differs between Moonraker versions, detected by what the running
Moonraker has (never by its version number, which old builds don't know).

Current Moonraker (a `moonraker` package, 2023 on) has moonraker.common
with JobEvent and KlippyState, sends job_state:state_changed, and takes a
callback in klippy_apis.subscribe_objects. Older ones (a flat
moonraker.py + components/, as on the Creality Hi and K1) have none of
those: the job state comes as job_state:<state> events, status updates as
server:status_update, and the Klippy connection isn't a component.
"""

from __future__ import annotations
import importlib.util
import inspect
import os
from enum import Enum
from typing import Any, Callable, Dict

def _common() -> Any:
    # Only when `moonraker` is a package: on a flat Moonraker the name is
    # moonraker.py, the server's own script, not to be imported twice
    try:
        spec = importlib.util.find_spec("moonraker")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        try:
            from moonraker import common
            # A 2023 package-layout Moonraker before JobEvent existed has
            # common.py without it: that one sends the old events too
            if hasattr(common, "JobEvent") and hasattr(common, "KlippyState"):
                return common
        except ImportError:
            pass
    try:
        # A flat Moonraker (moonraker.py + components/) that already has
        # common.py: it is on sys.path as a top-level module
        import common  # type: ignore[import-not-found]
        if hasattr(common, "JobEvent") and hasattr(common, "KlippyState"):
            return common
    except ImportError:
        pass
    return None


_mod = _common()
# Whether this Moonraker has JobEvent (and with it job_state:state_changed)
HAS_JOB_EVENT = _mod is not None
if _mod is not None:
    JobEvent = _mod.JobEvent
    KlippyState = _mod.KlippyState
else:

    class KlippyState(Enum):  # type: ignore[no-redef]
        """moonraker.common.KlippyState, for Moonraker without it."""
        DISCONNECTED = 1
        STARTUP = 2
        READY = 3
        ERROR = 4
        SHUTDOWN = 5

    class JobEvent(Enum):  # type: ignore[no-redef]
        """moonraker.common.JobEvent, for Moonraker without it."""
        STANDBY = 1
        STARTED = 2
        PAUSED = 3
        RESUMED = 4
        COMPLETE = 5
        CANCELLED = 6
        ERROR = 7

        def __str__(self) -> str:
            return self.name.lower()

        @property
        def finished(self) -> bool:
            return self.value >= 5

        @property
        def aborted(self) -> bool:
            return self.value >= 6

        @property
        def is_printing(self) -> bool:
            return self.value in (2, 4)

# The events older Moonraker sends instead of job_state:state_changed, each
# with (prev_stats, new_stats)
OLD_JOB_EVENTS: Dict[str, Any] = {
    f"job_state:{e.name.lower()}": e for e in JobEvent}


def register_job_events(server: Any, handler: Callable[..., Any]) -> None:
    """Call handler(JobEvent, prev_stats, new_stats) on every job state
    change, whichever way this Moonraker reports it. JobEvent and
    job_state:state_changed arrived together, so having JobEvent tells
    which one it is (listening to both could report a change twice)."""
    if HAS_JOB_EVENT:
        server.register_event_handler("job_state:state_changed", handler)
        return
    for name, event in OLD_JOB_EVENTS.items():
        def relay(prev: Dict[str, Any], new: Dict[str, Any],
                  _event: Any = event) -> Any:
            return handler(_event, prev, new)
        server.register_event_handler(name, relay)


def takes_status_callback(kapis: Any) -> bool:
    """Whether klippy_apis.subscribe_objects takes a callback (else status
    updates come as the server:status_update event)."""
    try:
        params = inspect.signature(kapis.subscribe_objects).parameters
    except (TypeError, ValueError, AttributeError):
        return True
    return "callback" in params or "cb" in params or len(
        [p for p in params.values()
         if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 3


def klippy_shutdown(server: Any) -> bool:
    """Whether Klipper is in its shutdown state."""
    conn = None
    try:
        conn = server.lookup_component("klippy_connection")
    except Exception:
        conn = getattr(server, "klippy_connection", None)
    state = getattr(conn, "state", None)
    if state is None:
        try:
            state = server.get_klippy_state()
        except Exception:
            return False
    if isinstance(state, Enum):
        return state.name == "SHUTDOWN"
    return str(state).lower() == "shutdown"


def config_dir(app_args: Dict[str, Any], data_path: str) -> str:
    """Where pushraker.conf goes: next to moonraker.conf when that is in
    the data path (printer_data/config on a standard install), else
    <data path>/config. Some printers keep moonraker.conf in the firmware
    (the Creality Hi: /usr/share/moonraker), which is no place for user
    settings."""
    conf = app_args.get("config_file") or ""
    here = os.path.dirname(conf)
    if here:
        try:
            inside = os.path.commonpath(
                [os.path.realpath(here), os.path.realpath(data_path)]) \
                == os.path.realpath(data_path)
        except ValueError:
            inside = False
        if inside:
            return here
    return os.path.join(data_path, "config")
