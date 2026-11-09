#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The settings page against the real component, on a stand-in U1 (the
test suite's fakes): for looking at the page and clicking through it.

    python3 tests/serve_page.py [port]     # http://127.0.0.1:7813/
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.argv = sys.argv[:1] + sys.argv[1:2]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pushraker as T  # noqa: E402  (sets up the stand-in Moonraker)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7813
WEB = os.path.join(HERE, "..", "src", "pushraker", "web", "index.html")


def u1_state():
    T.reset_world()
    T.STATE.update({
        "heater_bed": {"temperature": 54.7, "target": 60.0, "power": 0.62},
        "temperature_sensor cavity": {"temperature": 31.2},
        "temperature_host host": {"temperature": 48.0},
        "tmc2240 stepper_x": {"temperature": 48.1},
        "tmc2240 stepper_y": {"temperature": 46.3},
        "tmc2209 stepper_z": {"temperature": None, "run_current": 0.8},
        "fan": {"speed": 0.55, "rpm": None},
        "heater_fan e0_nozzle_fan": {"speed": 1.0, "rpm": 6100},
        "fan_generic cavity_fan": {"speed": 0.3, "rpm": None},
        "purifier": {"exhaust_fan": {"speed": 0.4}, "inner_fan":
                     {"speed": 1.0}, "inner_fan_rpm": 3200},
        "homing_precise_corexy": {"use_calibration_origin": True},
        "output_pin cavity_led": {"value": 1.0},
        "filament_switch_sensor e0_filament": {"filament_detected": True,
                                               "enabled": True},
        "machine_state_manager": {"main_state": 0, "action_code": 0},
        "ace": {"head_source": {"1": {"ace_index": 0, "slot": 2}},
                "aces": []},
    })
    for i, n in enumerate(("extruder", "extruder1", "extruder2",
                           "extruder3")):
        T.STATE[n] = {"temperature": 219.8 if i != 2 else 25.0,
                      "target": 220.0 if i != 2 else 0.0, "power": 0.31,
                      "nozzle_diameter": 0.4}
    T.STATE["print_task_config"] = {
        "extruder_map_table": [0, 1, 2, 3],
        "filament_type": ["PLA", "PETG", "TPU", "NONE"],
        "filament_vendor": ["Snapmaker", "Elegoo", "eSun", ""],
        "filament_sub_type": ["SnapSpeed", "", "LW", ""],
        "filament_color_rgba": ["FFFFFFFF", "000000FF", "F98C36FF", ""],
        "filament_exist": [True, True, True, False],
        "filament_spool_id": [0, 0, 0, 0]}
    T.STATE["print_stats"] = T.ps("standby")


async def make():
    u1_state()
    server = T.Server()
    data = os.path.join(T.WORK, "data")
    os.makedirs(os.path.join(data, "config"), exist_ok=True)
    c = T.pn.PushNotifications(T.Config(server, dict(name="U1-martha")))
    await server.handlers["server:klippy_ready"]()
    c._ace_on = True
    return server, c


def local_codemirror(self, log=None):
    """CodeMirror from this checkout's codemirror/ folder (no download)."""
    import shutil as sh
    js, info = self._cm_paths()
    if log is not None:
        log.append("Copying from this checkout's codemirror/ (test server)")
    os.makedirs(self._cm_dir, exist_ok=True)
    sh.copy(os.path.join(HERE, "..", "codemirror", T.pn.CODEMIRROR["file"]),
            js)
    with open(info, "w") as f:
        json.dump({"version": T.pn.CODEMIRROR["version"],
                   "sha256": T.pn.CODEMIRROR["sha256"]}, f)


T.pn.PushNotifications._cm_download_sync = local_codemirror

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
SERVER, COMP = asyncio.run_coroutine_threadsafe(make(), LOOP).result()


def call(path, method, args):
    handler = SERVER.endpoints.get(path)
    if handler is None:
        return 404, {"error": {"code": 404, "message": "not here"}}
    try:
        res = asyncio.run_coroutine_threadsafe(
            handler(T.WebRequest(method, args)), LOOP).result(60)
        return 200, {"result": res}
    except Exception as e:
        return getattr(e, "status_code", 500), \
            {"error": {"code": 400, "message": str(e)}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/server/") or path.startswith("/machine/"):
            from urllib.parse import parse_qsl, urlsplit
            args = dict(parse_qsl(urlsplit(self.path).query))
            return self._send(*call(path, "GET", args))
        self._send(200, open(WEB, "rb").read(), "text/html")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        args = json.loads(self.rfile.read(n) or b"{}")
        self._send(*call(self.path.split("?")[0], "POST", args))


print(f"http://127.0.0.1:{PORT}/", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
