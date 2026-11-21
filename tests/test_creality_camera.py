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


def pframe(tag: bytes) -> bytes:
    """A stand-in P frame (one non-IDR slice)."""
    return b"\x00\x00\x00\x01\x41\x9a" + tag


class FakeStream:
    """cam_app's live stream: a Unix socket server sending `script` (a list
    of bytes, or a float to pause) to each client, then holding the
    connection (`then`: "hold" or "close")."""

    def __init__(self, path, script, then="hold", refuse=False):
        import socket as _s
        self.path, self.script, self.then = path, script, then
        self.refuse = refuse
        self.clients = 0
        self.closed_by_client = 0
        self.srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        import socket as _s
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except (_s.timeout, OSError):
                continue
            self.clients += 1
            if self.refuse:         # "Support 6 socket client only!"
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        try:
            for item in self.script:
                if isinstance(item, float):
                    time.sleep(item)
                else:
                    conn.sendall(item)
            if self.then == "hold":
                t0 = time.monotonic()
                while time.monotonic() - t0 < 10 and not self.stop:
                    conn.sendall(pframe(b"PPP"))
                    time.sleep(0.066)
        except OSError:
            self.closed_by_client += 1
        finally:
            conn.close()

    def close(self):
        self.stop = True
        self.srv.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


def run_stream_tests(c, work, tl, calls, refer_switch, cap):
    """The live stream first; `capture 0` only when it gives nothing."""
    sockdir = tempfile.mkdtemp(dir="/tmp", prefix="crs")
    path = os.path.join(sockdir, "h264_uds")
    c.h264_uds = path
    c.capture_bin = cap
    limit, reuse = creality.STREAM_LIMIT, creality.REUSE
    creality.STREAM_LIMIT = 1.0
    creality.REUSE = 0
    c._last_picture = None
    refer_switch(1)
    orig = frame(b"L01")

    def fresh():
        with open(tl, "wb") as f:
            f.write(orig)
        os.utime(tl, (3000000, 3000000))
        if os.path.exists(calls):
            os.remove(calls)

    def untouched():
        return open(tl, "rb").read() == orig \
            and os.stat(tl).st_mtime == 3000000 \
            and not os.path.exists(calls)
    try:
        # Mid-stream: P frames, then a keyframe, then P frames
        fs = FakeStream(path, [pframe(b"P01")[3:], pframe(b"P02"),
                               frame(b"UDS"), pframe(b"P03")])
        fresh()
        t0 = time.monotonic()
        data, why = c.snapshot("camera", {"state": "printing"})
        took = time.monotonic() - t0
        check("live stream: its keyframe, nothing written, no capture",
              data == b"\xff\xd8\xffUDS" and untouched(), (data, why))
        check("...at once, also printing (no wait for Creality's frame)",
              took < 0.8, took)
        check("...a fresh picture: no age", getattr(data, "taken", None)
              is None)
        time.sleep(0.3)
        check("...the connection closed again (the server's next send "
              "fails)", fs.closed_by_client == 1, fs.closed_by_client)
        # After a print, during Creality's video: still the stream, no wait
        d = os.path.join(c.proc, "88")
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"/usr/bin/ffmpeg\x00-i\x00" + tl.encode() + b"\x00")
        fresh()
        t0 = time.monotonic()
        data, why = c.snapshot("camera", {"state": "complete",
                                          "since_end": 0.5})
        took = time.monotonic() - t0
        shutil.rmtree(d)
        check("...just after a print, the video being made: the stream at "
              "once, the file untouched", data == b"\xff\xd8\xffUDS"
              and took < 0.8 and untouched(), (data, took))
        fs.close()

        # A slow sender: the keyframe in pieces, within the limit
        kf = frame(b"SLW")
        fs = FakeStream(path, [pframe(b"P01"), kf[:6], 0.3, kf[6:12], 0.3,
                               kf[12:], pframe(b"P02")])
        fresh()
        data, why = c.snapshot("camera", {"state": "standby",
                                          "since_end": None})
        check("slow sender: the keyframe put together",
              data == b"\xff\xd8\xffSLW" and untouched(), (data, why))
        fs.close()

        # No keyframe within the limit: `capture 0` instead
        fs = FakeStream(path, [pframe(b"P01")])
        fresh()
        t0 = time.monotonic()
        data, why = c.snapshot("camera", {"state": "standby",
                                          "since_end": None})
        took = time.monotonic() - t0
        check("no keyframe in time: the capture fallback",
              data == b"\xff\xd8\xffNEW" and open(calls).read()
              == "capture 0\n", (data, why))
        check("...after STREAM_LIMIT, not longer",
              creality.STREAM_LIMIT - 0.1 <= took
              < creality.STREAM_LIMIT + 1.5, took)
        frame_alone, why = c.stream_frame()
        check("...and why", frame_alone is None and "in time" in why, why)
        fs.close()

        # Refused (six clients already): fallback at once
        fs = FakeStream(path, [], refuse=True)
        fresh()
        t0 = time.monotonic()
        data, why = c.snapshot("camera", {"state": "standby",
                                          "since_end": None})
        took = time.monotonic() - t0
        check("refused (6 clients): the capture fallback, quickly",
              data == b"\xff\xd8\xffNEW" and took < 1.0 and fs.clients == 1,
              (data, took, fs.clients))
        fs.close()

        # The server closes mid-keyframe: fallback
        kf = frame(b"CUT")
        fs = FakeStream(path, [pframe(b"P01"), kf[:8]], then="close")
        fresh()
        data, why = c.snapshot("camera", {"state": "standby",
                                          "since_end": None})
        check("the stream closing mid-keyframe: the capture fallback",
              data == b"\xff\xd8\xffNEW", (data, why))
        fs.close()

        # Printing, the stream failing: the fallback's own rules (guarded)
        fs = FakeStream(path, [], refuse=True)
        fresh()
        data, why = c.snapshot("camera", {"state": "printing"})
        check("printing, the stream refused: capture, guarded restore",
              data == b"\xff\xd8\xffNEW" and open(tl, "rb").read() == orig,
              (data, why))
        fs.close()

        check("keyframe(): SPS..IDR up to the next frame",
              creality.keyframe(pframe(b"a") + frame(b"K") + pframe(b"b"))[0]
              == frame(b"K"))
        check("...incomplete: none, kept from the SPS on",
              creality.keyframe(pframe(b"a") + frame(b"K"))
              == (None, len(pframe(b"a"))))
    finally:
        creality.STREAM_LIMIT, creality.REUSE = limit, reuse
        c.h264_uds = os.path.join(work, "no-h264_uds")
        shutil.rmtree(sockdir, ignore_errors=True)
        if os.path.exists(tl):
            os.remove(tl)
        if os.path.exists(calls):
            os.remove(calls)


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
    # No live stream unless a case sets one up (the fallback's cases)
    c.h264_uds = os.path.join(work, "no-h264_uds")
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

    # Printing, timelapse on: a fresh frame of our own, the file put back
    # only when nothing but our frame was added
    refer_switch(1)
    orig = frame(b"L01") + frame(b"L02") + frame(b"L03")

    def fresh_file():
        with open(tl, "wb") as f:
            f.write(orig)
        os.utime(tl, (1000000, 1000000))
        if os.path.exists(calls):
            os.remove(calls)

    def untouched():
        return open(tl, "rb").read() == orig \
            and os.stat(tl).st_mtime == 1000000

    fresh_file()
    data, why = c.snapshot("camera", {"state": "printing", "since_end": None})
    check("printing, timelapse on: a fresh frame (`capture 0`)",
          data == b"\xff\xd8\xffNEW" and open(calls).read() == "capture 0\n",
          (data, why))
    check("...no collision: the file restored byte for byte, its time too",
          untouched(), os.path.getsize(tl))
    check("...a fresh picture: no age", getattr(data, "taken", None) is None)

    # Collision: two frames added (Creality's landed with ours): left alone
    both = os.path.join(work, "capture_both")
    script(both, f"""open({tl!r}, "ab").write({frame(b"CRE") + frame(b"NEW")!r})
""")
    c.capture_bin = both
    fresh_file()
    data, why = c.snapshot("camera", {"state": "printing"})
    check("collision (two frames added): the file left as it is, "
          "Creality's frame kept", open(tl, "rb").read()
          == orig + frame(b"CRE") + frame(b"NEW"), os.path.getsize(tl))
    check("...the picture is the newest frame", data == b"\xff\xd8\xffNEW",
          data)

    # Creality's `capture` seen while ours runs: one frame, still left
    other = os.path.join(work, "capture_other")
    script(other, f"""import os, time
d = os.path.join({proc!r}, "43")
os.makedirs(d)
open(os.path.join(d, "cmdline"), "wb").write(b"capture\\x000\\x00")
time.sleep(0.3)
open({tl!r}, "ab").write({frame(b"NEW")!r})
os.remove(os.path.join(d, "cmdline"))
os.rmdir(d)
""")
    c.capture_bin = other
    fresh_file()
    data, why = c.snapshot("camera", {"state": "printing"})
    check("Creality capturing during ours: left as it is",
          data == b"\xff\xd8\xffNEW"
          and open(tl, "rb").read() == orig + frame(b"NEW"), (data, why))

    # A layer change during ours (Creality's capture follows): left alone
    slowcap = os.path.join(work, "capture_slowish")
    script(slowcap, f"""import time
time.sleep(0.9)
open({tl!r}, "ab").write({frame(b"NEW")!r})
""")
    c.capture_bin = slowcap
    fresh_file()
    st = {"print_stats": {"state": "printing", "info": {}},
          "virtual_sdcard": {"layer": 3, "layer_count": 0}}
    c.adjust_status({"virtual_sdcard": {"layer": 3}}, st)
    c._layer_changed -= 100     # as if layer 3 started long ago

    def next_layer():
        # After the quiet wait (SETTLE_STEADY), while ours runs
        time.sleep(creality.SETTLE_STEADY + 0.3)
        st["virtual_sdcard"]["layer"] = 4
        c.adjust_status({"virtual_sdcard": {"layer": 4}}, st)
    th = threading.Thread(target=next_layer)
    th.start()
    data, why = c.snapshot("camera", {"state": "printing"})
    th.join()
    check("a layer change during ours: left as it is",
          data == b"\xff\xd8\xffNEW"
          and open(tl, "rb").read() == orig + frame(b"NEW"), (data, why))

    # Hold-off: just after a layer change, wait LAYER_HOLD before capturing
    c.capture_bin = cap
    fresh_file()
    st["virtual_sdcard"]["layer"] = 5
    c.adjust_status({"virtual_sdcard": {"layer": 5}}, st)
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "printing"})
    took = time.monotonic() - t0
    check("a layer just changed: ours waits until LAYER_HOLD after it",
          creality.LAYER_HOLD - 0.1 <= took < creality.FRAME_WAIT + 1,
          took)
    check("...then a fresh frame, the file restored",
          data == b"\xff\xd8\xffNEW" and untouched(), (data, why))
    c._layer_changed -= 100

    # Creality's `capture` still running after FRAME_WAIT: go, leave it
    os.makedirs(os.path.join(proc, "42"))
    with open(os.path.join(proc, "42", "cmdline"), "wb") as f:
        f.write(b"/usr/bin/capture\x000\x00")
    fresh_file()
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "printing"})
    took = time.monotonic() - t0
    shutil.rmtree(os.path.join(proc, "42"))
    check("Creality's capture running throughout: ours after FRAME_WAIT, "
          "the file left as it is", data == b"\xff\xd8\xffNEW"
          and creality.FRAME_WAIT - 0.1 <= took
          and open(tl, "rb").read() == orig + frame(b"NEW"), (data, took))

    # Our capture fails: Creality's last frame, read only, with its time
    bad = os.path.join(work, "capture_bad")
    script(bad, "import sys\nsys.exit(1)\n")
    c.capture_bin = bad
    fresh_file()
    data, why = c.snapshot("camera", {"state": "printing"})
    check("our capture fails: Creality's last frame instead",
          data == b"\xff\xd8\xffL03" and untouched(), (data, why))
    check("...with when it was taken (the file's time: the age line)",
          getattr(data, "taken", None) == 1000000,
          getattr(data, "taken", None))
    os.remove(tl)
    data, why = c.snapshot("camera", {"state": "printing"})
    check("...and no frame at all: none, with the capture's reason",
          data is None and "didn't take a picture" in why, why)
    c.capture_bin = cap
    data, why = c.snapshot("camera", {"state": "printing"})
    check("no file yet while printing: a fresh frame, the file removed "
          "again", data == b"\xff\xd8\xffNEW" and not os.path.exists(tl),
          (data, why))
    if os.path.exists(calls):
        os.remove(calls)

    # Printing, timelapse off: capture, then the file exactly as before
    refer_switch(0)
    with open(tl, "wb") as f:
        f.write(b"OLD")
    os.utime(tl, (1000000, 1000000))
    data, why = c.snapshot("camera", {"state": "printing"})
    check("printing, timelapse off: a captured frame",
          data == b"\xff\xd8\xffNEW", (data, why))
    check("...taken just now (no age)",
          getattr(data, "taken", None) is None)
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
    # Just after a print: a fresh frame once Creality's video is made
    os.remove(calls)
    refer_switch(1)
    end = frame(b"F01") + frame(b"END")

    def end_file():
        with open(tl, "wb") as f:
            f.write(end)
        os.utime(tl, (2000000, 2000000))
        if os.path.exists(calls):
            os.remove(calls)

    def assembler(pid="77", start=0., run=1.):
        """Creality's ffmpeg making the video from the file, for `run` s
        from `start` s on."""
        def go():
            time.sleep(start)
            d = os.path.join(proc, pid)
            os.makedirs(d)
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"/usr/bin/ffmpeg\x00-framerate\x0015\x00-i\x00"
                        + tl.encode() + b"\x00-vcodec\x00copy\x00")
            time.sleep(run)
            shutil.rmtree(d)
        th = threading.Thread(target=go)
        th.start()
        return th

    def end_untouched():
        return open(tl, "rb").read() == end \
            and os.stat(tl).st_mtime == 2000000

    end_file()
    th = assembler(start=0.4, run=1.0)
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "complete", "since_end": 0.1})
    took = time.monotonic() - t0
    th.join()
    check("print complete: waits for Creality's video (starting 0.4 s "
          "later, 1 s long), then a fresh frame", data == b"\xff\xd8\xffNEW"
          and took >= 1.4 and open(calls).read() == "capture 0\n",
          (data, why, took))
    check("...the file restored exactly", end_untouched())
    end_file()
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "complete", "since_end": 0.2})
    took = time.monotonic() - t0
    check("...no video made within ASSEMBLY_DUE (timelapse empty, say): "
          "a fresh frame after it", data == b"\xff\xd8\xffNEW"
          and creality.ASSEMBLY_DUE - 0.4 <= took
          < creality.ASSEMBLY_DUE + 1.5 and end_untouched(), (data, took))
    end_file()
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "complete", "since_end": 30.})
    took = time.monotonic() - t0
    check("...later on (the video made by then): a fresh frame at once",
          data == b"\xff\xd8\xffNEW" and took < 1.5 and end_untouched(),
          (data, took))
    end_file()
    data, why = c.snapshot("camera", {"state": "standby", "since_end": None})
    check("...also when Moonraker restarted meanwhile (the file is new)",
          data == b"\xff\xd8\xffNEW" and end_untouched(), why)
    # Cancelled: Creality's video comes 8-26 s later; fresh frame now. The
    # assembly starting during ours: the file left as it is
    end_file()
    th = assembler(start=0.4, run=1.5)
    data, why = c.snapshot("camera", {"state": "cancelled", "since_end": 1.})
    th.join()
    check("print cancelled, the video starting during ours: fresh frame, "
          "the file left as it is", data == b"\xff\xd8\xffNEW"
          and open(tl, "rb").read() == end + frame(b"NEW"), (data, why))
    # The video takes longer than ASSEMBLY_MAX: the last frame, read only
    end_file()
    amax = creality.ASSEMBLY_MAX
    creality.ASSEMBLY_MAX = 0.5
    th = assembler(run=1.5)
    time.sleep(0.2)
    data, why = c.snapshot("camera", {"state": "complete", "since_end": 5.})
    th.join()
    creality.ASSEMBLY_MAX = amax
    check("the video still being made after ASSEMBLY_MAX: the last frame, "
          "read only, with its time", data == b"\xff\xd8\xffEND"
          and not os.path.exists(calls) and end_untouched()
          and getattr(data, "taken", None) == 2000000, (data, why))
    refer_switch(0)
    end_file()
    t0 = time.monotonic()
    data, why = c.snapshot("camera", {"state": "complete", "since_end": 0.1})
    check("...timelapse off: no wait for a video", data ==
          b"\xff\xd8\xffNEW" and time.monotonic() - t0 < 1.5
          and end_untouched(), (data, why))
    os.remove(tl)
    os.remove(calls)
    data, why = c.snapshot("camera", {"state": "complete",
                                      "since_end": 0.1})
    check("...no file (no video pending): a capture, the file removed again",
          data == b"\xff\xd8\xffNEW" and not os.path.exists(tl)
          and open(calls).read() == "capture 0\n", (data, why))
    check("assembling(): only a process reading the timelapse file",
          not c.assembling())
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

    run_stream_tests(c, work, tl, calls, refer_switch, cap)

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
