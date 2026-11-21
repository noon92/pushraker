# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Creality's own firmware (the Hi; its CFS is the `box` object, the spool
holder's RFID reader `filament_rack`). Its layers need nothing here: the
generic Printer reads virtual_sdcard's layer fields, which Creality's
Klipper adds.

The model, in this order:
1. Klipper's hostname, "Creality <model>-<last 4 hex of the MAC>" ("Creality
   Hi-78E7"): Creality's default, the model's own name as Creality writes
   it, almost always left as it is.
2. A printer renamed in Creality's UI: Creality's model code in its
   system_config.json (device_info.model_str), looked up in MODEL_CODES
   (kept minimal).
3. "Creality printer".
"""

from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from . import Printer

RX_HOSTNAME = re.compile(r"(Creality [A-Za-z0-9 +]+?)-[0-9A-Fa-f]{4}")
# Where Creality's firmware keeps system_config.json: the Hi (and its
# OpenWrt kin) under /mnt/UDISK, the K1 family under /usr/data
SYSTEM_CONFIGS = ("/mnt/UDISK/creality/userdata/config/system_config.json",
                  "/usr/data/creality/userdata/config/system_config.json")
# Creality model codes (device_info.model_str). Only codes seen first-hand
# or in a reliable source, with where:
MODEL_CODES: Dict[str, str] = {
    # The Hi's own system_config.json (model 1013) and its Klipper config
    # folder F018_CR4NU200360C20, on noon's printer, 2026-11-21
    "F018": "Creality Hi",
}


# The camera (WebRTC only, no snapshot address; findings-rev4). Creality's
# `capture 0` makes cam_app append one H.264 frame to the timelapse file;
# the firmware's own ffmpeg decodes it.
CAPTURE = "/usr/bin/capture"
# Creality's own live view (WebRTC): a page at / on port 8000 that plays the
# camera in a plain browser (the Hi; confirmed by noon, 2026-11-21). The
# notification's Live Camera link, on the Printer URL's host
WEBRTC = "/usr/bin/webrtc_local"
LIVE_STREAM = ":8000/"
TIMELAPSE = "/mnt/UDISK/timelapse/main_output.h264"
# delay_image.switch: Creality's timelapse on (1) or off (0); read by its
# Klipper (virtual_sdcard.get_delay_photography_info) for every capture
PRINT_REFER = "/mnt/UDISK/creality/userdata/config/user_print_refer.json"
# Creality turns the frames into the timelapse video 7-8 minutes after a
# print ends: the file isn't touched before then
VIDEO_SETTLE = 600.
TAIL_BYTES = 2 * 1024 * 1024
DECODE_TIMEOUT = 5.
# `capture` signals cam_app through a FIFO: the frame may land after it
# returns. The file is read and restored once its size has held this long
SETTLE_STEADY = 0.3
SETTLE_MAX = 3.
# One picture at a time (two notifications at once would interleave their
# captures and restores), and one taken this recently is reused
_LOCK = threading.Lock()
REUSE = 5.
CAMERA_NAME = "Printer camera"


def _sps_starts(data: bytes) -> list:
    """Where each H.264 sequence parameter set (NAL type 7) starts: every
    `capture` frame is self-contained (SPS, PPS, IDR)."""
    out, i = [], 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            return out
        if data[i + 3] & 0x1F == 7:
            out.append(i - 1 if i > 0 and data[i - 1] == 0 else i)
        i += 3


def last_frame(data: bytes) -> Optional[bytes]:
    """The last self-contained frame (from the last SPS) of raw H.264."""
    starts = _sps_starts(data)
    return data[starts[-1]:] if starts else None


class Creality(Printer):
    name = "creality"
    model_name = "Creality printer"
    system_configs: Sequence[str] = SYSTEM_CONFIGS
    capture_bin = CAPTURE
    webrtc_bin = WEBRTC
    timelapse = TIMELAPSE
    print_refer = PRINT_REFER
    proc = "/proc"

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(objects)
        # The firmware's own ffmpeg (the Hi has one): no ffmpeg, no camera
        self.ffmpeg = shutil.which("ffmpeg") or (
            "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else None)

    # -- the camera --

    def cameras(self) -> Dict[str, Dict[str, str]]:
        if self.ffmpeg and os.path.exists(self.capture_bin):
            live = LIVE_STREAM if os.path.exists(self.webrtc_bin) else ""
            return {CAMERA_NAME: {"snapshot": "printer:camera",
                                  "stream": live}}
        return {}

    def timelapse_on(self) -> bool:
        """Creality's timelapse switch; on when it can't be read (then the
        timelapse file is never touched during a print)."""
        try:
            with open(self.print_refer, encoding="utf-8") as f:
                data = json.load(f)
            return bool(int((data.get("delay_image") or {})
                            .get("switch", 1)))
        except (OSError, ValueError, TypeError, AttributeError):
            return True

    def capturing(self) -> bool:
        """Whether a `capture` is running (Creality's own, for its
        timelapse)."""
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            return False
        for pid in pids:
            try:
                with open(os.path.join(self.proc, pid, "cmdline"),
                          "rb") as f:
                    argv0 = f.read().split(b"\0", 1)[0]
            except OSError:
                continue
            if os.path.basename(argv0) == b"capture":
                return True
        return False

    def snapshot(self, name: str, ctx: Dict[str, Any]
                 ) -> Tuple[Optional[bytes], str]:
        """One picture at a time; the last one again within REUSE seconds."""
        with _LOCK:
            last = getattr(self, "_last_picture", None)
            if last and time.monotonic() - last[0] < REUSE:
                return last[1], ""
            data, why = self._snapshot(ctx)
            if data:
                self._last_picture = (time.monotonic(), data)
            return data, why

    def _snapshot(self, ctx: Dict[str, Any]
                  ) -> Tuple[Optional[bytes], str]:
        """The camera's picture by the printer's state (ctx: "state" =
        print_stats.state, "since_end" = seconds since a print ended or
        None; the component gives a huge number when none ended since it
        started, which the timelapse file's own time then covers):
        - printing with timelapse on, or within VIDEO_SETTLE after a print
          (Creality makes the video then): the newest frame in the
          timelapse file, read only (Creality's own last picture);
        - otherwise (printing with timelapse off, idle, or just ended with
          no file, so no video pending): `capture 0`, then the timelapse
          file put back exactly as it was; never while a `capture` runs.
        None only when neither gives a frame, with the reason."""
        if not self.ffmpeg:
            return None, "no ffmpeg on this printer"
        state = str(ctx.get("state") or "")
        printing = state in ("printing", "paused")
        since = ctx.get("since_end")
        try:
            # Also after a Moonraker restart, which forgets the print
            written = time.time() - os.path.getmtime(self.timelapse)
        except OSError:
            written = None
        just_ended = not printing and any(
            t is not None and t < VIDEO_SETTLE for t in (since, written))
        # Where touching Creality's timelapse file isn't safe (its video is
        # being recorded, or made from it now), its newest frame is read,
        # read only: Creality's own last picture
        if (printing and self.timelapse_on()) or just_ended:
            frame = self._last_timelapse_frame()
            if frame is not None:
                return self.decode(frame)
            if printing or os.path.exists(self.timelapse):
                return None, ("no camera frame yet (Creality takes one per "
                              "layer)" if printing else
                              "the printer is making its timelapse video "
                              "and its last frame couldn't be read")
            # Just ended, but no file: no video pending, a capture is safe
        if self.capturing():
            return None, "the printer is taking a timelapse frame"
        return self.capture_frame()

    def _last_timelapse_frame(self) -> Optional[bytes]:
        try:
            size = os.path.getsize(self.timelapse)
            with open(self.timelapse, "rb") as f:
                f.seek(max(0, size - TAIL_BYTES))
                tail = f.read()
        except OSError:
            return None
        return last_frame(tail)

    def capture_frame(self) -> Tuple[Optional[bytes], str]:
        """`capture 0`, the new frame decoded, the timelapse file restored
        (cut back to its size, or removed if it wasn't there)."""
        path = self.timelapse
        before = os.path.getsize(path) if os.path.exists(path) else None
        times = (os.stat(path).st_atime, os.stat(path).st_mtime) \
            if before is not None else None
        failed = ""
        new = b""
        try:
            try:
                r = subprocess.run([self.capture_bin, "0"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   timeout=DECODE_TIMEOUT)
                if r.returncode:
                    failed = f"exit {r.returncode}"
            except (OSError, subprocess.TimeoutExpired) as e:
                failed = str(e)
            # Whatever happened, wait for cam_app's write to land, so the
            # restore below takes all of it back
            self.settle(path)
            if not failed:
                try:
                    with open(path, "rb") as f:
                        f.seek(before or 0)
                        new = f.read()
                except OSError as e:
                    failed = str(e)
        finally:
            try:
                if before is None:
                    if os.path.exists(path):
                        os.remove(path)
                elif os.path.getsize(path) > before:
                    with open(path, "r+b") as f:
                        f.truncate(before)
                    os.utime(path, times)    # as it was, to the second
            except OSError:
                pass
        if failed:
            return None, f"the camera didn't take a picture ({failed})"
        frame = last_frame(new) or (new or None)
        if not frame:
            return None, "the camera took no picture"
        return self.decode(frame)

    @staticmethod
    def settle(path: str) -> None:
        """Until the file's size has held for SETTLE_STEADY seconds (at most
        SETTLE_MAX)."""
        def size() -> int:
            try:
                return os.path.getsize(path)
            except OSError:
                return -1
        end = time.monotonic() + SETTLE_MAX
        last, since = size(), time.monotonic()
        while time.monotonic() < end:
            time.sleep(0.05)
            now = size()
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= SETTLE_STEADY and now >= 0:
                return

    def decode(self, h264: bytes) -> Tuple[Optional[bytes], str]:
        """One H.264 frame to a JPEG with the firmware's ffmpeg, at low
        priority, in a temporary folder that is removed again (pictures of
        the room never stay on the printer)."""
        tmp = tempfile.mkdtemp(prefix="pushraker_cam_")
        try:
            src = os.path.join(tmp, "frame.h264")
            out = os.path.join(tmp, "frame.jpg")
            with open(src, "wb") as f:
                f.write(h264)
            r = subprocess.run(
                [str(self.ffmpeg), "-hide_banner", "-loglevel", "error",
                 "-y", "-f", "h264", "-i", src, "-frames:v", "1",
                 "-q:v", "4", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=DECODE_TIMEOUT,
                preexec_fn=lambda: os.nice(10))
            if r.returncode or not os.path.exists(out):
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                return None, "ffmpeg couldn't decode the frame" + \
                    (f" ({err[-120:]})" if err else "")
            with open(out, "rb") as f:
                data = f.read()
            return (data, "") if data.startswith(b"\xff\xd8") else \
                (None, "ffmpeg wrote no JPEG")
        except subprocess.TimeoutExpired:
            return None, "decoding the frame took too long"
        except OSError as e:
            return None, f"ffmpeg: {e}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        return "box" in objects or "filament_rack" in objects

    def model_code(self) -> Optional[str]:
        for path in self.system_configs:
            try:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f).get("device_info") or {}
            except (OSError, ValueError, AttributeError):
                continue
            code = str(info.get("model_str") or "").strip()
            if code:
                return code
        return None

    def model(self, hostname: str = "") -> str:
        m = RX_HOSTNAME.fullmatch(hostname.strip())
        if m:
            return m.group(1)
        code = self.model_code()
        return MODEL_CODES.get(code or "", self.model_name)
