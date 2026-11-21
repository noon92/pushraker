#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The Creality Hi's camera (printers/creality.py) with a fake `capture`
and a fake ffmpeg: which way each state takes, and that the timelapse
file is left exactly as it was. With a real ffmpeg (if installed), the
last self-contained frame of an H.264 stream decodes.

  python3 tests/test_creality_camera.py
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from pushraker.printers import creality  # noqa: E402

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


SPS = b"\x00\x00\x00\x01\x67"


def frame(tag: bytes) -> bytes:
    """A stand-in self-contained frame: SPS, PPS, IDR, then a tag."""
    return SPS + b"\x42" + b"\x00\x00\x00\x01\x68\xce" + \
        b"\x00\x00\x00\x01\x65\x88" + tag


def script(path, body):
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def run_creality_camera_tests():
    print("The Creality Hi's camera (capture + the firmware's ffmpeg)")
    work = tempfile.mkdtemp(prefix="creality_cam_")
    tl = os.path.join(work, "main_output.h264")
    refer = os.path.join(work, "user_print_refer.json")
    calls = os.path.join(work, "calls.txt")
    cap = os.path.join(work, "capture")
    script(cap, f"""import sys
open({calls!r}, "a").write("capture " + " ".join(sys.argv[1:]) + "\\n")
open({tl!r}, "ab").write({frame(b"NEW")!r})
""")
    ff = os.path.join(work, "ffmpeg")
    script(ff, """import sys
a = sys.argv
src = open(a[a.index("-i") + 1], "rb").read()
open(a[-1], "wb").write(b"\\xff\\xd8\\xff" + src[-3:])
""")
    proc = os.path.join(work, "proc")
    os.makedirs(proc)
    c = creality.Creality(["box"])
    c.ffmpeg, c.capture_bin, c.timelapse = ff, cap, tl
    c.print_refer, c.proc = refer, proc
    # Each case below takes its own picture (the reuse is tested at the end)
    reuse = creality.REUSE
    creality.REUSE = 0

    def refer_switch(on):
        with open(refer, "w") as f:
            f.write('{"delay_image": {"switch": %d}}' % on)

    c.webrtc_bin = os.path.join(work, "no-webrtc_local")
    check("the camera is offered when ffmpeg and capture exist",
          c.cameras() == {"Printer camera": {"snapshot": "printer:camera",
                                             "stream": ""}})
    c.webrtc_bin = cap   # any file that exists
    check("with Creality's webrtc_local: the live page on port 8000",
          c.cameras()["Printer camera"]["stream"] == ":8000/")
    from pushraker import settings as _settings
    _s = {"printer_url": "http://192.168.0.220/", "camera": "Printer camera"}
    check("its Live Camera link is port 8000 on the Printer URL's host",
          _settings.camera_live_url(dict(_settings.defaults(), **_s),
                                    _settings.Env(cameras=c.cameras()))
          == "http://192.168.0.220:8000/")
    nof = creality.Creality(["box"])
    nof.ffmpeg = None
    check("no ffmpeg: no camera", nof.cameras() == {}
          and nof.snapshot("camera", {"state": "printing"})[0] is None)

    # Printing, timelapse on: the last frame, read only
    refer_switch(1)
    with open(tl, "wb") as f:
        f.write(frame(b"L01") + frame(b"L02") + frame(b"L03"))
    old = os.stat(tl)
    data, why = c.snapshot("camera", {"state": "printing", "since_end": None})
    check("printing, timelapse on: the newest frame Creality took",
          data == b"\xff\xd8\xffL03", (data, why))
    check("...read only: no capture, the file untouched",
          not os.path.exists(calls) and os.stat(tl).st_size == old.st_size
          and os.stat(tl).st_mtime == old.st_mtime)
    os.remove(tl)
    data, why = c.snapshot("camera", {"state": "printing"})
    check("...no frame yet (before the first layer): none, and why",
          data is None and "per layer" in why, why)

    # Printing, timelapse off: capture, then the file exactly as before
    refer_switch(0)
    with open(tl, "wb") as f:
        f.write(b"OLD")
    os.utime(tl, (1000000, 1000000))
    data, why = c.snapshot("camera", {"state": "printing"})
    check("printing, timelapse off: a captured frame",
          data == b"\xff\xd8\xffNEW", (data, why))
    check("...the timelapse file cut back, its time too",
          open(tl, "rb").read() == b"OLD"
          and os.stat(tl).st_mtime == 1000000)
    os.remove(tl)
    os.remove(calls)
    # Idle, long after a print: capture, the new file removed again
    data, why = c.snapshot("camera", {"state": "standby",
                                      "since_end": 3600.})
    check("idle: a captured frame, the file it made removed",
          data == b"\xff\xd8\xffNEW" and not os.path.exists(tl), why)
    check("...`capture 0`, once", open(calls).read() == "capture 0\n")
    # Just after a print (Creality is making the video): its last frame,
    # read only
    os.remove(calls)
    with open(tl, "wb") as f:
        f.write(frame(b"F01") + frame(b"END"))
    data, why = c.snapshot("camera", {"state": "complete",
                                      "since_end": 120.})
    check("within 10 minutes of a print's end: the last frame, read only",
          data == b"\xff\xd8\xffEND" and not os.path.exists(calls),
          (data, why))
    data, why = c.snapshot("camera", {"state": "standby", "since_end": None})
    check("...also when Moonraker restarted meanwhile (the file is new)",
          data == b"\xff\xd8\xffEND" and not os.path.exists(calls), why)
    os.remove(tl)
    data, why = c.snapshot("camera", {"state": "complete",
                                      "since_end": 120.})
    check("...no file (no video pending): a capture, the file removed again",
          data == b"\xff\xd8\xffNEW" and not os.path.exists(tl)
          and open(calls).read() == "capture 0\n", (data, why))
    # Creality's own capture running
    os.makedirs(os.path.join(proc, "42"))
    with open(os.path.join(proc, "42", "cmdline"), "wb") as f:
        f.write(b"/usr/bin/capture\x000\x00")
    data, why = c.snapshot("camera", {"state": "standby", "since_end": None})
    check("a `capture` already running: none", data is None
          and "timelapse frame" in why, why)
    shutil.rmtree(os.path.join(proc, "42"))
    # Unreadable switch: treated as on (the file is never touched)
    os.remove(refer)
    check("switch unreadable: timelapse on", c.timelapse_on())

    # capture returns before cam_app's frame lands (it only signals a FIFO)
    late = os.path.join(work, "capture_late")
    script(late, f"""import os, sys, time
open({calls!r}, "a").write("late\\n")
if os.fork() == 0:
    time.sleep(0.5)
    open({tl!r}, "ab").write({frame(b"LAT")!r})
    os._exit(0)
""")
    c.capture_bin = late
    refer_switch(0)
    data, why = c.snapshot("camera", {"state": "standby", "since_end": None})
    time.sleep(0.8)
    check("a frame that lands after capture returned: read, then the file "
          "removed again", data == b"\xff\xd8\xffLAT"
          and not os.path.exists(tl), (data, why))
    # capture fails after writing: the file is still put back
    slow = os.path.join(work, "capture_slow")
    script(slow, f"""import time
open({tl!r}, "ab").write({frame(b"SLO")!r})
time.sleep(3)
""")
    with open(tl, "wb") as f:
        f.write(b"KEEP")
    old_timeout = creality.DECODE_TIMEOUT
    creality.DECODE_TIMEOUT = 0.5
    c.capture_bin = slow
    data, why = c.snapshot("camera", {"state": "standby", "since_end": None})
    creality.DECODE_TIMEOUT = old_timeout
    check("capture timing out: no picture, the file restored all the same",
          data is None and open(tl, "rb").read() == b"KEEP", (why,))
    os.remove(tl)
    # Two notifications at once: one capture, one picture for both
    creality.REUSE = reuse
    c.capture_bin = cap
    c._last_picture = None
    if os.path.exists(calls):
        os.remove(calls)
    got = []
    threads = [threading.Thread(target=lambda: got.append(c.snapshot(
        "camera", {"state": "standby", "since_end": None})))
        for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("two at once: one capture, the same picture for both, the file "
          "left as it was", len(got) == 2 and got[0][0] == got[1][0]
          == b"\xff\xd8\xffNEW" and open(calls).read() == "capture 0\n"
          and not os.path.exists(tl), (got, open(calls).read()))

    # A real ffmpeg, a real stream: each frame with its own headers
    real = shutil.which("ffmpeg")
    if real:
        h = os.path.join(work, "real.h264")
        r = subprocess.run(
            [real, "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc=size=320x240:rate=1", "-frames:v", "3", "-c:v",
             "libx264", "-g", "1", "-x264-params", "repeat-headers=1",
             "-f", "h264", h], capture_output=True)
        if r.returncode == 0:
            data = open(h, "rb").read()
            last = creality.last_frame(data)
            c.ffmpeg = real
            jpg, why = c.decode(last)
            check("real ffmpeg: the last self-contained frame decodes to a "
                  "JPEG", jpg is not None and jpg.startswith(b"\xff\xd8")
                  and len(last) < len(data), why)
        else:
            print("  (skipped: this ffmpeg has no libx264)")
    else:
        print("  (skipped: no ffmpeg here)")
    left = [n for n in os.listdir(tempfile.gettempdir())
            if n.startswith("pushraker_cam_")]
    check("no decoded pictures left behind", not left, left)
    shutil.rmtree(work, ignore_errors=True)
    return _failures


if __name__ == "__main__":
    run_creality_camera_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
