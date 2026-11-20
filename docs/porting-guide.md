# Porting pushraker to another printer: a guide for agents

What was learned bringing pushraker to the Creality Hi: stock firmware, a
2022 Moonraker, done over SSH. Checklists first; the reasons are short.

## 0. Ground rules

- [ ] **Read-only first.** Survey, read, then ask. Every change on the printer goes in a change log with how to undo it; back up a file before changing it (`<file>.bak-pushraker`).
- [ ] **Never run a vendor binary with arguments to "see what it does".** On the Hi, `cam_app -h` meant "height", not help: it touched the camera's files. Read files, configs, `strings` (Python's `re.findall(rb"[\x20-\x7e]{4,}", data)` works without binutils), and the HTTP GETs the vendor's own UI makes.
- [ ] **Never open `/dev/video*`** while the vendor's camera service holds it.
- [ ] **Check `print_stats.state` before anything that restarts Moonraker or Klipper**, every time. Never start, pause or cancel a print, move anything or heat anything.
- [ ] **Never change, upgrade or remove the vendor's packages** (Moonraker's Python, `/usr/lib/python3.*`, opkg/apt). Old firmware is held together by exactly those versions.
- [ ] **Permission refusals are final.** If your tool refuses an action (e.g. writing into the firmware area), don't route around it. Stage the files and let the printer's owner run the one command; that is also the real acceptance test.
- [ ] Mask secrets (tokens, passwords, webhook URLs, e-mail addresses) in everything you write down. Logs on the printer contain them.
- [ ] Note the printer's clock and time zone (`date`). The Hi ran on Moscow time while its owner was two time zones away.

## 1. Survey (read-only)

Find out, and save the raw output:
- [ ] **Moonraker**: running? `/server/info` (`api_version`, `moonraker_version` is often `"?"` on vendor builds), its command line in `/proc/<pid>/cmdline` (`-c`, `-d`, `-l`), `HOME` in `/proc/<pid>/environ` (no `-d` means the data path is `$HOME/printer_data`), how it starts (systemd unit, procd `/etc/init.d/*`, BusyBox `S??` scripts).
- [ ] **Layout**: flat (`moonraker.py` + `components/`, before Feb 2023) or packaged (`moonraker/moonraker/components`). Is `moonraker.conf` inside the data path or in the firmware (the Hi: `/usr/share/moonraker/moonraker.conf`)?
- [ ] **Python** Moonraker runs with, and its `apprise`, `jinja2`, `requests`, `PIL` versions. Is `pip` there? The Hi has no curl or wget: use `python3 -c "import urllib.request …"`.
- [ ] **Filesystems**: `mount`, `df`. Squashfs plus overlay means firmware files are writable but wiped by an update; find the user-data partition (the Hi: `/mnt/UDISK`).
- [ ] **Web UI**: who serves it (nginx config in firmware?), which ports.
- [ ] **Klipper objects** (`/printer/objects/list`) and a status query of the interesting ones (layers, filament sensors, multi-material boxes, the vendor's own objects).
- [ ] **Cameras**: Moonraker webcams; anything on 8080/8081; the vendor's camera process and what it serves.
- [ ] **G-code files**: which slicer wrote them, whether they write `SET_PRINT_STATS_INFO`, thumbnail blocks, and Moonraker's `/server/files/metadata` for one of them.

**Never date a printer's Moonraker by the printer's age.** Vendors freeze old Moonraker on new hardware (the Hi, 2025: API 1.0.5 code from 2022). Detect features, not versions.

## 2. Old Moonraker: what `compat.py` already covers

| Missing on old Moonraker | pushraker's way (feature-detected) |
|---|---|
| `moonraker.common` (JobEvent, KlippyState) | fallback enums; a package `common.py` without `JobEvent` counts as missing |
| `job_state:state_changed` | the per-state events `job_state:started/paused/resumed/complete/cancelled/error/standby` with `(prev, new)` |
| `subscribe_objects(..., callback)` | `server:status_update` (all components' subscriptions merged: extra objects and fields arrive) |
| `klippy_connection` as a component | `server.klippy_connection`, `get_klippy_state()` |
| `register_endpoint(content_type=, auth_required=)` | `wrap_result=False` returns a str, which tornado sends as text/html |
| metadata for the Orca family (OrcaSlicer, Creality Print, Snapmaker Orca, Bambu Studio) | `thumbs.py` reads the file's own thumbnail; no estimated time or layer count from Moonraker |

Floor: Moonraker with the `job_state` component (Nov 2021 on), Python 3.7. Below that, `install.py` refuses and says what is missing.

## 3. Where things go

- [ ] Settings: `<data_path>/config/pushraker.conf`, also when `moonraker.conf` lives in the firmware. Secrets: `<data_path>/pushraker.secrets`.
- [ ] Code: a folder in user data (`install.py`: `pushraker-install/` next to the data folder, with its marker file).
- [ ] **Exactly two things in the firmware area**, if `moonraker.conf` and `components/` live there: one component file (it adds the code folder to the **end** of `sys.path`, so the vendor's packages win) and the `[pushraker]` section. A firmware update removes both; re-running the install command restores them, and the settings survive.
- [ ] Never edit the vendor's nginx or macros. The settings page is served by Moonraker: `http://<printer>:7125/server/pushraker/page`.

## 4. Dependencies

- [ ] Run `tests/check_deps.py` with **Moonraker's own Python** on the printer: `<python> check_deps.py <dir holding pushraker/>`. It builds every service's request with the network replaced by recorders, and **asserts what the server needs** (Authorization header or credentials, topic or chat, priority, picture). A request that was merely built proves nothing: that's how the ntfy token bug slipped.
- [ ] Apprise 1.2.0 traps, both hit on the Hi:
  - custom schema names are **2–9 characters** (`discordhook://` never registered; it's `discordwh://` now);
  - its ntfy has **no access tokens** (posts anonymously → 403). `ntfy.py` (`pntfy://`) takes over where the installed Apprise can't take a token.
- [ ] Pillow 8.2: no QOI and no `load_default(size=)` (handled). Check images with check_deps.
- [ ] A missing package: `install.py` refuses and names it. Never shadow a package the vendor has.

## 5. Printer differences live in `printers/`

- [ ] Values the user sees (`job.*`, `pushraker.*`, templates) stay universal; only where they come from is per printer.
- [ ] Generic `Printer` hooks: `status_fields` + `adjust_status` (fill a standard value from the printer's own fields), `missing_rows()` (hide rows for absent hardware; never change saved settings), `cameras()` (the printer's own camera), `model()` (backups' "from").
- [ ] Layers: Creality's Klipper counts them in `virtual_sdcard.layer`/`layer_count`. The generic `Printer` fills `print_stats.info` from them wherever the fields exist; a slicer's `SET_PRINT_STATS_INFO` wins.
- [ ] A profile is detected by objects only the vendor has (Creality: `box`, `filament_rack`), never by hostname.
- [ ] Model name: the vendor's stock hostname pattern ("Creality Hi-78E7") first, a short model-code table only for renamed printers.

## 6. Cameras

- [ ] `discover.py` probes 127.0.0.1 at Klippy ready (8080/8081 `?action=snapshot`, `/snapshot`, `/webcam/`) and accepts only real JPEG/PNG bytes. That covers mjpg-streamer, ustreamer/crowsnest and camera-streamer.
- [ ] WebRTC-only vendors (Creality Hi, K1C 2025, K2): no HTTP snapshot. On the Hi, `capture 0` writes an H.264 frame **into the timelapse file**, and the firmware's own `/usr/bin/ffmpeg` decodes it (0.8 s). See findings-rev4. Never decode WebRTC in pushraker.

## 7. Testing on the printer without touching the vendor's Moonraker

- [ ] **A second Moonraker on another port**: copy the vendor's Moonraker source to user data, give it its own config (port 7126, its own data path, a copy of the database so it doesn't re-parse G-code files), point it at Klipper's socket, start it only while idle, stop it after each test. It runs the vendor's exact Moonraker code with your component, and touches nothing of theirs.
- [ ] Busybox: no `nohup` or `timeout`; start with `( cmd & echo $! > pid )`. `grep` has no `--include`.
- [ ] Load the page in headless Chrome from the Mac (`--dump-dom`, `--screenshot`) to catch JS errors that only real data triggers.

## 8. `install.py` (the one command) and its safety rules

- [ ] Finds Moonraker from `/proc`; refuses with several unless `--pid`.
- [ ] Refuses below the floor and names exactly what is missing.
- [ ] Wheel from PyPI with its sha256 checked, or `--wheel` for an unreleased build; unpacked atomically.
- [ ] **Marker file** in its folder: it never deletes a folder without it, and refuses to install into a non-empty folder without it.
- [ ] Restarts only the init script or unit that **names this Moonraker's script or config** (otherwise `/server/restart`); never while printing.
- [ ] Waits for pushraker in `/server/info` (and the Klipper connection only if it was there before), else **rolls back** and says so.
- [ ] `--uninstall` works with Moonraker down (from the paths the marker recorded), and never acts on a different Moonraker.

## 9. Working with a lead (mailbox)

- [ ] After each stage, write the next `mail/from-agent-NNN.md`: what was done, what was found, what's next, `QUESTION:` lines. Then read the new `to-agent` files; newer instructions win.
- [ ] Mail the **cause before the fix** when asked. Don't wait for an answer unless it blocks everything.
- [ ] Write every file with a revision suffix (`-rev1`, `-rev2`); never overwrite a sent mail.
