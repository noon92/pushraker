# Porting pushraker to a new printer

For a person or an AI agent with SSH access to a Klipper + Moonraker printer
and this repository. It assumes no knowledge of the project's history.
Placeholders look like `<this>`.

> **If you are an AI agent**
> - Work **read-only** until the printer's owner says otherwise. Read files, logs and HTTP GETs; change nothing.
> - **Ask the owner before** anything that restarts Moonraker or Klipper, installs or removes anything, writes outside your own scratch folder, or runs a vendor binary. Never start, pause or cancel a print, move an axis or heat anything unless the owner explicitly asks for that one action.
> - A tool or permission refusal is final: don't route around it. Stage the files and give the owner the one command to run.
> - **Report** after the survey (section 3), after each decision you can't make from the tables (section 4), and before any change on the printer: what you found, what you propose, how to undo it.
> - **Stop and ask** when: the printer is printing; Moonraker has no `job_state` component; the only route needs writing into the firmware beyond the two items in section 1; a step would replace or upgrade a vendor package; you can't tell what a binary or script does.
> - Never print, store or commit secrets (tokens, passwords, webhook URLs, e-mail addresses): logs and configs on printers contain them. Never keep camera pictures of the room.

## Contents

1. [What a port is](#1-what-a-port-is)
2. [Ground rules](#2-ground-rules)
3. [Survey](#3-survey)
4. [Decision tables](#4-decision-tables)
5. [The printer profile API](#5-the-printer-profile-api)
6. [Testing](#6-testing)
7. [install.py](#7-installpy)
8. [Known traps](#8-known-traps)
9. [Done checklist](#9-done-checklist)
10. [Worked example: the Creality Hi](#10-worked-example-the-creality-hi)

## 1. What a port is

- **The same package on every printer.** pushraker detects what it can: Klipper's objects, Moonraker's features, cameras, web pages, slicer metadata. Detection beats configuration.
- **The harness is two things**, on every printer: one component file in Moonraker's `components/` folder, and a `[pushraker]` section in `moonraker.conf`. Everything else lives outside the firmware:
  - the code: `pushraker-install/` next to Moonraker's data folder (install.py), or site-packages (pip);
  - the settings: `<data_path>/config/pushraker.conf` and `<data_path>/pushraker.secrets`.
- **A printer profile** (`src/pushraker/printers/<vendor>.py`) only for what can't be detected: a vendor camera, vendor objects that carry standard values (e.g. layers), the model name, the web UI address, vendor faults or calibrations.
- **Not a port:** changing templates or the `job.*` / `pushraker.*` values per printer (they stay universal; only where a value comes from differs), editing the vendor's nginx or macros, replacing the vendor's Moonraker or Python packages.
- Many printers need **no profile at all**: the generic `Printer` covers stock Klipper.

## 2. Ground rules

- [ ] **Read-only first.** Survey, read, report, then change.
- [ ] **Change log.** Every change on the printer: file, what, how to undo. Back up a file before changing it (`<file>.bak-pushraker`).
- [ ] **Never change the vendor's packages**: Moonraker's Python, `/usr/lib/python3*`, opkg/apt. Old firmware depends on exactly those versions.
- [ ] **Never run a vendor binary with arguments you don't understand.** `-h` may not mean help: read `strings`, configs and the vendor's own scripts instead. Without binutils: `python3 -c "import re,sys; print(b'\n'.join(re.findall(rb'[\x20-\x7e]{4,}', open(sys.argv[1],'rb').read())).decode())" <binary>`.
- [ ] **Never open `/dev/video*`** while the vendor's camera process holds it.
- [ ] **Check `print_stats.state` before anything that restarts Moonraker or Klipper**, every time:
  `python3 -c "import json,urllib.request as u; print(json.load(u.urlopen('http://127.0.0.1:7125/printer/objects/query?print_stats=state'))['result']['status']['print_stats']['state'])"`
- [ ] **No prints, no motion, no heating.** The owner runs or explicitly OKs anything that restarts services.
- [ ] Note the printer's clock and time zone (`date`): vendor clocks are often UTC or another zone. Convert before reporting times.

## 3. Survey

Run on the printer and save the output (read-only; `sh -s` takes the script from stdin):

```sh
ssh <user>@<printer> 'sh -s' < survey.sh > survey-<printer>.txt 2>&1
```

`survey.sh`:

```sh
#!/bin/sh
# pushraker survey: read-only. Writes, installs and restarts nothing.
sec() { echo; echo "===== $1 ====="; }
py=python3
get() { $py -c "import sys,urllib.request as u; print(u.urlopen(sys.argv[1], timeout=8).read().decode('utf-8','replace'))" "$1" 2>&1; }
sec system;      uname -a; cat /etc/os-release /etc/openwrt_release 2>/dev/null; date; busybox 2>&1 | head -1
sec disk;        df -h 2>/dev/null; mount | awk '{print $1, $3, $5, $6}'
sec processes;   ps w 2>/dev/null || ps aux
sec ports;       (netstat -tlnp 2>/dev/null || ss -tlnp 2>/dev/null) | head -40
sec moonraker;   for p in $(pgrep -f moonraker 2>/dev/null); do echo "pid $p:"; tr '\0' ' ' < /proc/$p/cmdline; echo; tr '\0' '\n' < /proc/$p/environ | grep -E '^(HOME|PATH|MOONRAKER)'; ls -l /proc/$p/exe; done
sec server_info; get http://127.0.0.1:7125/server/info
sec printer;     get http://127.0.0.1:7125/printer/info
sec objects;     get http://127.0.0.1:7125/printer/objects/list
sec webcams;     get http://127.0.0.1:7125/server/webcams/list
sec python;      for p in $(for q in $(pgrep -f moonraker); do readlink /proc/$q/exe; done | sort -u); do echo "$p: $($p -V 2>&1)"; $p -c "
for m in ('apprise','jinja2','requests','PIL','tornado'):
    try:
        x=__import__(m); print(m, getattr(x,'__version__','?'))
    except Exception as e: print(m, 'MISSING', e)"; done
sec web;         ls /etc/nginx /etc/nginx/conf.d 2>/dev/null; grep -h -E 'listen|root|location|proxy_pass' /etc/nginx/nginx.conf /etc/nginx/conf.d/* /etc/nginx/sites-enabled/* 2>/dev/null | head -60
sec camera;      ls -l /dev/video* /dev/v4l/by-id 2>/dev/null; ps w 2>/dev/null | grep -i -E 'cam|mjpg|ustream|stream|webrtc|go2rtc' | grep -v grep
sec reach;       $py -c "import urllib.request as u; print(u.urlopen('https://pypi.org/pypi/pushraker/json', timeout=8).status)" 2>&1
```

Record, and what each answer decides:

| Record | Where | Decides |
|---|---|---|
| Moonraker running, its pid, command line (`-c`, `-d`, `-l`), `HOME` | `ps`, `/proc/<pid>/cmdline`, `environ` | data path (`-d`, else `$HOME/printer_data`), config path, install route |
| `api_version`, `components` list (needs `job_state`) | `/server/info` (don't save `/server/config`: it can hold other components' secrets) | supported or not (§4.1) |
| Layout: flat `moonraker.py` + `components/`, or the `moonraker/` package | the executable's path | compat paths (§4.1) |
| Moonraker's Python and its packages | `/proc/<pid>/exe`, imports | §4.2 |
| How Moonraker restarts | systemd unit, `/etc/init.d/*`, BusyBox `S??` | install.py's restart (§7) |
| Filesystems: squashfs/overlay vs user partition | `mount`, `df` | what survives a firmware update (§1) |
| Web server, its ports and `/server/` proxy | nginx config, `netstat` | page route, web page URL (§4.3, §4.6) |
| Klipper objects | `/printer/objects/list` | profile detection (`matches`), layers, calibrations, chamber, drivers (§4.4–4.5) |
| Cameras: Moonraker webcams, ports 8080/8081, vendor camera processes | `/server/webcams/list`, `ps`, ports | §4.3 |
| Slicer of the user's files; `SET_PRINT_STATS_INFO`; thumbnails; Moonraker's metadata for one file | the G-code head, `/server/files/metadata?filename=<file>` | layers, render, time left (§4.4) |
| Model name: hostname pattern, vendor config files | `/printer/info` hostname, vendor configs | `model()` (§4.7) |
| PyPI reachable | the `reach` section | install from PyPI or `--wheel` |

**Never date a Moonraker by the printer's age.** Vendors ship old Moonraker on new hardware. Detect features, not versions.

## 4. Decision tables

### 4.1 Moonraker

| Found | Then |
|---|---|
| no Moonraker | stop: not supported (installing Moonraker is the owner's call) |
| Moonraker without `job_state` (before Nov 2021) | stop: not supported; install.py refuses and says why |
| flat layout (`moonraker.py` + `components/`, before Feb 2023) | supported: `compat.py` covers it (table below) |
| packaged layout (`moonraker/moonraker/components`) | supported, nothing special |
| `moonraker.conf` inside the firmware (not under the data path) | supported: install.py edits it (backed up) and re-running it restores after a firmware update |

What `compat.py` covers on old Moonraker:

| Missing | pushraker's way (feature-detected) |
|---|---|
| `moonraker.common` (`JobEvent`, `KlippyState`) | fallback enums |
| `job_state:state_changed` | the per-state events `job_state:started/paused/…` |
| `subscribe_objects(..., callback)` | `server:status_update` |
| `klippy_connection` as a component | `server.klippy_connection`, `get_klippy_state()` |
| `register_endpoint(content_type=, auth_required=)` | plain text/html result |
| Orca-family metadata (OrcaSlicer, Creality Print, Snapmaker Orca, Bambu Studio): "Unknown" slicer, no thumbnail | `gmeta.py` (time, layers, filament from the file's comments) and `thumbs.py` (the file's own thumbnail); Moonraker's values win where it has them |

### 4.2 Dependencies (Moonraker's Python)

| Package | Needed | If missing or older |
|---|---|---|
| Python | ≥ 3.7 | not supported |
| apprise | ≥ 1.2 | install.py refuses and names it; never add or upgrade it yourself |
| Jinja2 | ≥ 3.0 | same |
| requests | any | same |
| Pillow | optional, ≥ 8.2 | without: no spool pictures or preview images |

Apprise 1.2.x works, with two quirks pushraker handles (§8). Verify with `check_deps.py` on the printer's own Python (§6.1).

### 4.3 Camera

| Found | Then |
|---|---|
| a Moonraker webcam with a snapshot URL that returns a JPEG/PNG | nothing to build: offered as is |
| a camera server Moonraker doesn't list (mjpg-streamer, ustreamer/crowsnest, camera-streamer on 8080/8081 or nginx `/webcam/`) | nothing to build: `discover.py` finds it at Klippy ready ("Camera (port 8080)") |
| a webcam whose snapshot gives no picture, or an `iframe` webcam | not offered (unless already chosen) |
| vendor camera with no HTTP snapshot (WebRTC-only, vendor IPC) | profile: `cameras()` + `snapshot()` (§5.5). Cheapest first: the vendor's live stream if it can be read passively; the vendor's capture command; a decoder already in the firmware (ffmpeg); never a WebRTC client; never a big download |
| a vendor live-view page in a browser | its port/path as the camera's `stream` (`":8000/"`): the Live Camera link on the Printer URL's host |
| nothing | no camera: notifications use the render or no image |

### 4.4 Layers, time, render

| Found | Then |
|---|---|
| the slicer writes `SET_PRINT_STATS_INFO` | layers come from `print_stats.info`: nothing to build |
| the vendor's Klipper counts layers elsewhere (Creality: `virtual_sdcard.layer`/`layer_count`) | generic `adjust_status` already copies those two fields; another vendor's fields: `status_fields` + `adjust_status` (§5.3) |
| no layer source | layer notifications aren't sent; progress, time left and status still work |
| Moonraker's metadata has the estimate and thumbnails | nothing to build |
| metadata "Unknown"/empty for the user's slicer | `gmeta.py`/`thumbs.py` already read the file; check with one real file |

### 4.5 Calibrations and hardware rows

| Found | Then |
|---|---|
| `bed_mesh`, `z_tilt` or `quad_gantry_level` | generic pre-print watch (`preprint.py`): *Pre-print calibrations complete* at the first layer, with what ran and how long. Nothing to build |
| the vendor announces the calibrations a job will run (a status object) | profile: `preprint_calibrations` + `preprint_codes` (§5.8): also listed in *Print started* |
| touchscreen calibrations, coded faults, power-loss records, toolhead maps, MMUs | profile attributes (§5.9); otherwise those rows stay unavailable |
| no `heater_generic`/purifier/DragonBreath | chamber rows hidden (`missing_rows`) |
| no TMC2240/TMC5160 on X/Y | *Driver running hot* hidden |
| no `heater_bed` | *Bed cooled* hidden |

### 4.6 Settings page and web page URL

| Found | Then |
|---|---|
| any printer | the page is served by Moonraker: `http://<printer>:7125/server/pushraker/page`. Nothing to build |
| nginx serving Fluidd/Mainsail, and you may edit its config (your own setup) | optionally `location /notifications/ { alias <…>/pushraker/web/; }` (README) |
| vendor nginx in the firmware | don't edit it; use Moonraker's route |
| the web UI's address is fixed for the model | profile: `web_page` + `web_page_name` (§5.6). It becomes the Printer URL's default and first Fill-in choice |
| not fixed | nothing: Fill in offers the web pages found on the printer (`discover.web_pages`), never Moonraker's port |

### 4.7 Model name (backups' "from", restore rules)

| Found | Then |
|---|---|
| the vendor's default hostname carries the model ("Creality Hi-78E7") | profile `model()`: parse it |
| renamed printers keep a model code in a vendor file | a short code → name table, only codes seen first-hand |
| neither | `model_name`, else the profile name |

## 5. The printer profile API

A profile is a subclass of `Printer` in `src/pushraker/printers/<vendor>.py`, imported at the bottom of `printers/__init__.py` and listed in `PRINTERS` (first match wins; the generic `Printer` otherwise). Keep it to what can't be detected.

```python
# src/pushraker/printers/acme.py
from typing import Any, Dict, Sequence
from . import Printer

class Acme(Printer):
    name = "acme"
    model_name = "Acme X1"

    @classmethod
    def matches(cls, objects: Sequence[str]) -> bool:
        return "acme_box" in objects          # an object only this vendor has
```

```python
# src/pushraker/printers/__init__.py (bottom)
from .acme import Acme  # noqa: E402
PRINTERS: Tuple[type, ...] = (SnapmakerU1, Creality, Acme)
```

### 5.1 `matches(objects)`

Detect by Klipper objects only the vendor has (never by hostname). Called with `/printer/objects/list` at Klippy ready.

### 5.2 `status_fields`

`{object: [fields]}` subscribed with the component's own. Klipper leaves out fields an object doesn't have.

```python
status_fields = {"virtual_sdcard": ["layer", "layer_count"], "acme_box": ["slot"]}
```

### 5.3 `adjust_status(data, status)`

Fill a **standard** value from the vendor's fields, in place in `status` (the merged status), after each update `data`. Call `super()` to keep the generic layer copy.

```python
def adjust_status(self, data, status):
    super().adjust_status(data, status)
    box = status.get("acme_box") or {}
    if "layer" in box:
        status.setdefault("print_stats", {})["info"] = {
            "current_layer": box["layer"], "total_layer": box.get("layers")}
```

### 5.4 Rows: `rows_unavailable`, `missing_rows()`

- `rows_unavailable`: rows the printer can never produce (left off the page, off by default). The generic default lists the U1-only ones; set `()` when your profile produces all.
- `missing_rows()` hides rows for absent hardware from the object list (chamber, drivers, bed). Override only for vendor hardware the generic table can't see; never change saved settings.

```python
rows_unavailable = ("calibration", "filament_change", "spool_switch",
                    "slot_empty", "power_loss", "warning")
```

### 5.5 Camera: `cameras()`, `snapshot()`, `Frame`, `frame_note`

- `cameras()` → `{name: {"snapshot": "printer:<id>", "stream": "<path, :port/path or URL>"}}`. A `printer:` snapshot is served by `snapshot()`.
- `snapshot(name, ctx)` → `(jpeg_bytes, "")` or `(None, "why")`. Blocking: runs in a thread. `ctx`: `state` (print_stats.state), `since_end` (seconds since a print ended, or None). Put the reason in plain words: it is appended to the notification ("Error sending camera image: <why>").
- A picture taken earlier: return `Frame(jpeg, taken=<time.time() it was taken>)`. Over 60 s old, the text ends with "Camera image from 74 s ago: `<frame_note>`."
- `frame_note`: why it can be old ("Creality takes one per layer"), or `""` for the age alone.
- Never leave pictures on disk: decode in a temp folder, remove it in `finally`.

```python
from . import Frame, Printer

class Acme(Printer):
    frame_note = "the printer takes one per layer"

    def cameras(self):
        return {"Printer camera": {"snapshot": "printer:cam", "stream": ":8080/"}}

    def snapshot(self, name, ctx):
        jpeg, taken = read_vendor_frame()          # yours
        if jpeg is None:
            return None, "the camera is off"
        return Frame(jpeg, taken), ""
```

### 5.6 Web page: `web_page`, `web_page_name`

The web UI as a URL with `{host}` (the printer's address as the browser reaches it). The page uses it as the Printer URL's placeholder, first Fill-in choice, and fills it in on first setup and after restoring another model's backup.

```python
web_page = "http://{host}:4408/"
web_page_name = "Fluidd"
```

### 5.7 `model(hostname)`

The most specific model name ("Creality Hi"), from Klipper's hostname (`printer.info`) or a vendor file. Used in backups' "from": restoring another model's backup keeps this printer's Printer URL and camera.

```python
def model(self, hostname=""):
    m = re.fullmatch(r"(Acme [A-Z0-9]+)-[0-9A-F]{4}", hostname.strip())
    return m.group(1) if m else self.model_name
```

### 5.8 Pre-print calibrations

- Generic (default): `bed_mesh`/`z_tilt`/`quad_gantry_level` present and the profile declares none → reported at the first layer.
- A vendor that announces the job's calibrations: `preprint_calibrations = {<flag in a status object>: "<name>"}` and `preprint_codes = {<action code>: (<flag>, <toolhead or None>)}`, as in `snapmaker_u1.py`. They are then also listed in *Print started*.

### 5.9 Other attributes (see `printers/__init__.py` for each one's comment)

| Attribute / method | For |
|---|---|
| `exception_event`, `exception_text_paths`, `runout_exception_id` | vendor fault events with coded texts |
| `calibrations`, `msm_idle`, `msm_abnormal`, `manual_clean` | touchscreen calibrations and steps waiting for a person |
| `chamber_sensor`, `action_preheat_chamber` | chamber temperature and the vendor's chamber wait |
| `power_loss_env_paths` | power-loss recovery records |
| `extruders`, `toolhead_map`, `filament_extruder()`, `extruder_values()`, `extruder_catalogue()` | multi-toolhead filament, refills, MMU slots |
| `time_methods`, `time_default`, `left_seconds()` | the vendor's own time-left estimate |
| `hidden_objects`, `hint()`, `fan_catalogue()` | the template editor's object picker |
| `lift_ceiling` | the print-end snapshot lift's Z limit |

## 6. Testing

### 6.1 Dependencies on the printer's own Python

Copy `tests/check_deps.py` and the package to a temp folder on the printer, run it with **Moonraker's Python**, then remove the folder:

```sh
ssh <user>@<printer> 'mkdir -p /tmp/prk'
scp -r src/pushraker tests/check_deps.py <user>@<printer>:/tmp/prk/
ssh <user>@<printer> '<moonraker python> /tmp/prk/check_deps.py /tmp/prk; rm -rf /tmp/prk'
```

It builds every service's request through the installed Apprise with the network replaced by recorders (nothing leaves the printer), and asserts, per service:
- the request was built;
- **it carries what the server needs** (auth header or credentials, topic or chat, …);
- priority high and a picture attached where the service supports them;
- the webhook's JSON fields, and the email for STARTTLS, SSL and plain SMTP;
- Jinja and Pillow: templates load, the spool picture, problem picture and email card render.

A request that is merely built proves nothing.

### 6.2 In the repository

```sh
python3 tests/test_pushraker.py      # the component against a stand-in Moonraker (also runs test_settings.py, test_install.py)
python3 tests/test_settings.py
python3 tests/test_creality_camera.py tests/test_preprint.py tests/test_discover.py tests/test_ntfy.py
python3 tests/serve_page.py 7813     # the settings page in a browser, no printer
```

- A new profile: a test with the vendor's object list (detection, rows, status mapping), fake vendor files or servers for its camera (see `test_creality_camera.py`: fake `capture`, fake ffmpeg, a fake Unix-socket stream server with slow, refusing and closing variants).
- Page JS: `node --check` on the extracted `<script>`, and a load in headless Chrome to catch errors only real data triggers.

### 6.3 A second Moonraker on another port (no risk to the vendor's)

1. Copy the vendor's Moonraker source to user data.
2. Give it its own config: `port: 7126`, its own data path, a **copy** of the database (so it doesn't re-parse every G-code file), Klipper's socket.
3. Start it only while the printer is idle: `( <python> <copy>/moonraker.py -d <data> -c <conf> & echo $! > pid )` (BusyBox has no `nohup`).
4. Test, then stop it (`kill $(cat pid)`) and remove the copy.

It runs the vendor's exact Moonraker with your component and touches nothing of theirs.

### 6.4 On a real print (the owner starts it)

- [ ] *Print started* (render), *Pre-print calibrations complete* (what ran, how long), *First layer started*, *Layer complete*, 25/50/75 %, status reports
- [ ] *Print complete* with a **current** camera picture and the render; *Print cancelled* from a cancel
- [ ] *Bed cooled* after the print
- [ ] time left and ETA plausible; layer numbers match the file
- [ ] the vendor's own features still work (timelapse video, cloud view, live view)

## 7. install.py

The one command, over SSH, for printers whose Moonraker came with the firmware (no pip needed):

```sh
python3 -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/noon92/pushraker/main/install.py').read())"
```

What it detects and does:
- finds the running Moonraker from `/proc` (several: `--pid <pid>`): its Python, components folder (flat or packaged), `moonraker.conf`, data path, restart method;
- checks Python ≥ 3.7, `job_state`, apprise ≥ 1.2, Jinja2 ≥ 3.0, requests; refuses and names what's missing; never adds, upgrades or removes a package;
- gets the wheel from PyPI (sha256-checked) or `--wheel <file>`; unpacks into `pushraker-install/lib` next to the data folder, atomically;
- has the package write the component file and `[pushraker]` (`moonraker.conf` backed up as `moonraker.conf.bak-pushraker`);
- restarts Moonraker (never while printing), waits for pushraker to load, **rolls back** if it doesn't, prints the page URL.

Flags: `--uninstall` (`--purge`: settings and secrets too), `--version <x>`, `--wheel <file>`, `--name <printer name>`, `--install-dir <dir>`, `--klipper` (adds the print-end snapshot macro file and its `[include]`), `--no-restart`, `--pid <pid>`.

Safety rules it keeps:
- [ ] a **marker file** in its folder: it never deletes a folder without one, and refuses a non-empty folder without one;
- [ ] restarts only the init script or unit that names this Moonraker's script or config (else `/server/restart`);
- [ ] `--uninstall` works with Moonraker down, from the paths the marker recorded, and never acts on a different Moonraker.

Testing an unreleased build:

```sh
python3 -m pip wheel --no-deps -w dist .                       # on your machine
scp dist/pushraker-<version>-py3-none-any.whl install.py <user>@<printer>:<staging dir>/
ssh <user>@<printer> 'python3 <staging dir>/install.py --wheel <staging dir>/pushraker-<version>-py3-none-any.whl'
```

## 8. Known traps

| Symptom | Cause | Fix |
|---|---|---|
| a Discord-webhook URL "not supported" on an old printer | Apprise 1.2 custom schema names must be 2–9 characters | pushraker's schemas are `discordwh://`, `discorddm://`, `pntfy://` |
| ntfy 401/403 with a token that works elsewhere | Apprise 1.2's ntfy has no access-token support | `ntfy.py` sends ntfy itself when the installed Apprise can't take a token |
| a send fails and the log only says "(failed)" | Apprise logs the reason at its own level | pushraker captures Apprise's messages per service and shows the reason on the console |
| ETAs or log times an hour off | the printer's clock or zone differs from the user's | the page's **Time zone** setting; convert vendor log times when reporting |
| "Unknown" slicer, no render or estimate | old Moonraker's metadata doesn't parse the Orca family | `gmeta.py`/`thumbs.py` read the file (already on) |
| a restored backup's links open the old printer | Printer URL restored from another model | restore keeps this printer's Printer URL and camera when the backup's "from" model differs |
| Web UI button opens Moonraker's API | the page was served by Moonraker; its port isn't the web UI's | Fill in offers the profile's or the discovered web pages, never `:7125` |
| no camera picture, the camera "works in the browser" | WebRTC-only camera: no HTTP snapshot | a profile `snapshot()` (§5.5) |
| the vendor's live page is white for ~45 s | it waits for full ICE gathering before connecting (a STUN server) | not pushraker's; tell the user to wait or reload |
| pushraker gone after a firmware update | the firmware area (component file, `moonraker.conf`) is reset | re-run the install command; settings survive in user data |
| a tool refuses a write in the firmware area | your permission system | stage the files, the owner runs install.py |
| *Bed cooled* missing after an update | a Moonraker restart while the bed was hot | resumed at Klippy ready from Moonraker's job history |
| moonraker.log tracebacks "Command (ss -ltn) failed" | Moonraker's webcam `convert_local` needs `ss` | pushraker converts local addresses itself |
| a camera choice that never gives a picture | an iframe webcam or a dead snapshot URL | not offered unless already chosen |
| a camera frame shows a person | test captures | keep frames local, delete them, never commit or publish |

## 9. Done checklist

- [ ] Survey saved; decisions from §4 written down
- [ ] Profile (if any): `matches`, only the needed hooks, a test with the vendor's object list
- [ ] All test suites pass; page JS checks
- [ ] `check_deps.py` passes on the printer's own Python
- [ ] Installed with install.py (or pip); Moonraker log clean; `GET /server/pushraker/status` answers; the page loads; Send Test arrives with a picture and the right Web UI / Live Camera links
- [ ] A real print: the list in §6.4
- [ ] Docs: the guide's printer chapter, the README's compatibility list and feature chart
- [ ] Change log handed over: every change on the printer and how to undo it

## 10. Worked example: the Creality Hi

| Topic | Found | Done |
|---|---|---|
| Moonraker | 1.0.5-era flat layout in the firmware (`/usr/share/moonraker`), data in `/mnt/UDISK/printer_data`, procd init script | `compat.py`; install.py edits the firmware's `moonraker.conf` (backed up), code in `/mnt/UDISK/pushraker-install` |
| Python | 3.9, apprise 1.2.0, Pillow 8.2, no pip, no curl | the ntfy sender; `urllib` everywhere |
| Profile | objects `box`, `filament_rack` | `printers/creality.py` |
| Layers | `virtual_sdcard.layer`/`layer_count` (counted at layer markers); `layer_count` 0 for Orca files | generic `adjust_status`; total from the file (`gmeta`) |
| Camera | USB UVC camera encoding H.264; `cam_app` sends every frame to up to 6 clients of `/var/run/h264_uds`; live page on `:8000` (WebRTC) | `snapshot()` reads one keyframe from the socket (≤ 2.5 s), decodes with the firmware's ffmpeg; fallback: Creality's `capture`, with its timelapse file restored unless that would cut Creality's own frame; stream `":8000/"` for Live Camera |
| Calibrations | `START_PRINT` meshes the bed on Fluidd-started prints | generic pre-print watch |
| Web UI | Fluidd on 4408; port 80 is Creality's own service (404 at `/`) | `web_page = "http://{host}:4408/"` |
| Model | hostname "Creality Hi-78E7"; model code in `system_config.json` | `model()` |
| Hooks | no print hooks | *Print complete* / *Cancelled* from the job event |
