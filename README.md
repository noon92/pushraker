# pushraker

Push notifications for Klipper printers, as a Moonraker component: print
started, paused, resumed, complete, cancelled, progress, first layer, faults
and shutdowns, power-loss recovery, chamber temperature, filament (Spoolman,
backup spools, MMUs), with pictures (camera snapshot, model preview, spool
drawings). Every notification's title, contents and image can be edited
on the page, as Jinja templates over Klipper's values and pushraker's own
(print job, toolheads, filament, queue). Sent through [Apprise](https://github.com/caronc/apprise): ntfy,
Pushover, Gotify, Telegram, Discord, a webhook, email. Configured from its
own web page, live, with no Moonraker or Klipper restart.

It grew out of the Push Notifications feature of the
[Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware),
where it is built in. User documentation: the
[Push Notifications guide](docs/guide.md). Every template value:
[values](docs/values.md) ([by printer](docs/values-by-printer.md)). One
notification in a file, to share or to have an AI assistant write (with a
[prompt to paste](docs/notification-files.md#writing-one-with-an-ai-assistant)):
[notification files](docs/notification-files.md). Bringing it to another
printer: the [porting guide](docs/porting-guide.md).

## One package, many printers

- **Detection over configuration.** The same package runs on every
  printer and works out what it can at start-up: Klipper's objects,
  Moonraker's version and features, cameras (Moonraker's webcams and
  camera servers it doesn't list), the printer's web pages, and what the
  G-code files themselves say (render, estimate, layers, filament).
- **Printer profiles only where needed** (`src/pushraker/printers/`): for
  what can't be detected, such as a vendor camera with no snapshot address,
  the vendor's own fault codes and calibrations, the model name and the
  web UI's address. Printers without a profile get the generic one.
- **Two install routes:** built into the Snapmaker U1 Extended Firmware;
  or, on any Klipper printer with Moonraker, one command over SSH that
  leaves the vendor's packages alone (below). pip works too.

### Compatibility

| | |
|---|---|
| **Tested** | Snapmaker U1 (Extended Firmware, built in); Creality Hi (stock firmware, SSH install) |
| **Expected to work, untested** | any Klipper + Moonraker printer whose Moonraker has the `job_state` component (Nov 2021 on), with Python 3.7+, apprise 1.2+, Jinja2 3.0+ and requests: what the generic profile gives (chart below) |
| **Not supported** | Moonraker without `job_state`; firmware without Moonraker |

Untested, reports welcome (what is known, not a promise):
- Creality K1 / K1 Max / older K1C: camera at `:8080/?action=snapshot`, found by discovery. K1C (2025 firmware) and K2: WebRTC only, like the Hi.
- Sovol SV08, QIDI Q1 Pro, Flashforge AD5M with Klipper Mod: a camera server on 8080 that Moonraker doesn't list, found by discovery.
- Elegoo Neptune 4: mjpg-streamer once set up (`/webcam/?action=snapshot`).

### Features by printer

| Feature | Snapmaker U1 | Creality Hi | Other Klipper printers |
|---|---|---|---|
| Print started/paused/resumed/complete/cancelled, progress, status reports, errors, Klipper shutdown, macros | ✅ | ✅ | ✅ |
| Camera picture | ✅ | ✅ live stream | ➖ ¹ |
| Live Camera link | ✅ | ✅ | ➖ ¹ |
| Render, time left and ETA | ✅ | ✅ | ✅ |
| Layers (first layer, layer complete) | ✅ | ✅ | ➖ ² |
| Pre-print calibrations | ✅ listed in Started, reported when done | ✅ reported when done | ➖ reported when done ³ |
| Bed cooled | ✅ | ✅ | ✅ (heated bed) |
| Chamber temperature, driver temperature | ✅ | ❌ no such hardware | ➖ ⁴ |
| Filament: toolhead map, backup spools, MMU slots | ✅ | ❌ | ❌ |
| Filament: Spoolman (not enough, running low) | ✅ | ➖ with Spoolman | ➖ with Spoolman |
| Coded warnings, touchscreen calibrations, power-loss recovery | ✅ | ❌ | ❌ |
| Print-end and cancel snapshots (print hooks) | ✅ | ❌ ⁵ | ➖ ⁵ |
| Excluded objects | ✅ | ✅ | ✅ |
| Printer URL default (web UI) | ✅ | ✅ Fluidd :4408 | ➖ ⁶ |
| Settings page | `/notifications/` | `:7125/server/pushraker/page` | `:7125/server/pushraker/page`, or an nginx alias |

¹ A Moonraker webcam with a snapshot, or a camera server discovery finds (8080/8081, `/webcam/`); none for WebRTC-only cameras without a profile.
² When the slicer writes `SET_PRINT_STATS_INFO`, or the Klipper fork counts layers (Creality's `virtual_sdcard.layer`).
³ Bed mesh, Z tilt, quad gantry level from stock Klipper's messages, when the printer has them.
⁴ Chamber: any `heater_generic`, a DragonBreath. Driver: TMC2240/TMC5160 on X/Y.
⁵ *Print complete* and *Cancelled* are sent from the print's end event; the hooks need `_PRINT_END_NTFY`/`_CANCEL_PRINT_NTFY` called from your macros.
⁶ Fill in offers the web pages found on the printer.

Full chart: [guide](docs/guide.md#features-by-printer).

**Status: 0.3.** The component has been in use on the Snapmaker U1 for a
while; the page, the settings file, the installer and the multi-printer
support are new. Notifications a printer can't produce start off and are
left off the page; turn one on in `pushraker.conf` and it shows on the
page and is sent like any other.

## Requirements

- Moonraker with its `job_state` component (upstream since November 2021;
  the older flat `moonraker.py` layout is fine, as on the Creality Hi).
- The Python Moonraker runs with: 3.7 or newer with install.py (3.9 or
  newer for `pip install`), with apprise 1.2 or newer, Jinja2 3.0 or newer
  and requests. Pillow (8.2 or newer) is optional with install.py (pip
  installs it): without it notifications go without spool pictures and
  preview images.

## Install on a printer whose Moonraker came with its firmware (over SSH)

For printers like the Creality Hi, where Moonraker, its Python and nginx
belong to the firmware: log in over SSH and run

```sh
python3 -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/noon92/pushraker/main/install.py').read())"
```

It finds the running Moonraker, checks the requirements above (it never
adds, upgrades or removes a package of Moonraker's Python), downloads
pushraker from PyPI (sha256-checked), unpacks it next to Moonraker's data
folder (on the Hi: `/mnt/UDISK/pushraker-install`, marked as install.py's
own: it never deletes a folder it didn't make), adds one component file to
Moonraker's components folder and `[pushraker]` to `moonraker.conf`
(backed up first as `moonraker.conf.bak-pushraker`), restarts Moonraker
(never while printing), checks it loaded, and prints the settings page's
address: `http://<printer>:7125/server/pushraker/page`, served by Moonraker
itself, so no web server change is needed (through the printer's own web
interface's port only where its web server is set up for it, as below). Klipper's config is left alone
(`--klipper` adds the print-end snapshot macros).

Run the same command again to update, or after a firmware update removed
it: the settings are kept. Options go after the command
(`python3 -c "..." --uninstall`): `--uninstall` (also when Moonraker no
longer starts; `--purge` also removes the settings), `--version X`, `--wheel FILE`, `--name`, `--no-restart`;
`python3 install.py --help` lists them all.

### On the Creality Hi

The command above, over SSH as root. It puts one file in
`/usr/share/moonraker/components/` and `[pushraker]` in
`/usr/share/moonraker/moonraker.conf` (backed up first); pushraker itself
goes in `/mnt/UDISK/pushraker-install`, the settings in
`/mnt/UDISK/printer_data/config/pushraker.conf`. Settings page:
`http://<printer>:7125/server/pushraker/page`.

A firmware update removes the first two: run the command again; the
settings stay. On the Hi, pushraker works with Creality's older Moonraker
and Apprise, reads the render, estimate and filament from the G-code
files themselves, takes layers from Creality's own count, and takes
camera pictures from the Hi's camera through the firmware's ffmpeg. See
the [guide](docs/guide.md#creality-hi).

## Install (any Moonraker printer)

```sh
pip install pushraker            # into the Python Moonraker runs with
python -m pushraker install      # ~/moonraker and ~/printer_data by default
```

`install` puts a one-file shim into Moonraker's `components/` folder, adds
`[pushraker]` to `moonraker.conf`, and includes the Klipper macro file
from `printer.cfg` (`--no-klipper` skips it). Restart Moonraker and
Klipper; pushraker writes `printer_data/config/pushraker.conf` at its
first start. Open `http://<printer>:7125/server/pushraker/page`.
Optionally, serve the page from the nginx that serves Fluidd or Mainsail
too (`src/pushraker/web/index.html`, or `python -c "import pushraker, os;
print(os.path.dirname(pushraker.__file__) + '/web')"`), e.g.

```nginx
location /notifications/ {
    alias /path/to/site-packages/pushraker/web/;
    index index.html;
}
```

and open `http://<printer>/notifications/`. The page talks to Moonraker
through the `/server/` proxy of that nginx; without one it falls back to
Moonraker's own port, which needs the page's origin in `cors_domains`
under `[authorization]` in `moonraker.conf`. With Moonraker's
authorization on, the page asks for an API key the first time.

Call `_PRINT_END_NTFY` at the start of your `PRINT_END` macro and
`_CANCEL_PRINT_NTFY` at the start of `CANCEL_PRINT` for the print-end and
cancel snapshots to be taken before the printer parks (the Snapmaker U1
extended firmware does that through its hook system).

`python -m pushraker install --help` lists the paths and options (include
directories, the printer's default name, the error-code index link, moving
settings over from an INI section). `python -m pushraker uninstall` takes
it out again; `--purge` also removes the settings and secrets.

## Where things live on the printer

- `printer_data/config/pushraker.conf`: the settings, a plain INI file
  (also editable by hand; the component re-reads it when it changes).
- `printer_data/pushraker.secrets`: tokens and passwords, mode 0600,
  outside the config folder so Moonraker never serves it.
- Moonraker endpoints: `GET /server/pushraker/page` (the settings page),
  `GET/POST /server/pushraker/config`, `POST /server/pushraker/test`,
  `GET /server/pushraker/status`, `POST /server/pushraker/backup`, and the
  page's helpers (`objects`, `preview`, `preview_image`, `codemirror`,
  `codemirror_js`); all but the page also under
  `/server/push_notifications/`, the 0.1 names.

## Layout

- `src/pushraker/component.py` — the Moonraker component
- `src/pushraker/settings.py` — the settings: validation, storage, the
  Apprise URLs built from them
- `src/pushraker/printers/` — what differs between printers, one module
  per printer: `snapmaker_u1.py`, `creality.py` (the Creality Hi); the
  generic `Printer` in `__init__.py`
- `src/pushraker/compat.py` — older Moonraker versions
- `src/pushraker/discover.py` — camera servers and web pages on the printer
- `src/pushraker/preprint.py` — pre-print calibrations from stock Klipper
- `src/pushraker/gmeta.py`, `src/pushraker/thumbs.py` — estimate, layers,
  filament and the render from the G-code file itself
- `src/pushraker/ntfy.py` — ntfy with an access token on Apprise versions
  that can't send one
- `src/pushraker/values.py`, `values_map.py` — the values a template uses,
  and which printers have them (`values_map.py` and `docs/values*.md` are
  generated by `tests/gen_values.py`)
- `src/pushraker/images.py` — spool pictures (Pillow)
- `src/pushraker/email_html.py`, `src/pushraker/discord.py` — email card,
  Discord senders
- `src/pushraker/web/index.html` — the settings page
- `src/pushraker/klipper/pushraker.cfg` — the Klipper macro file
- `src/pushraker/__main__.py` — `python -m pushraker install|uninstall|status`
- `install.py` — the one-line install over SSH (standard library only; not
  part of the package)
- `docs/guide.md` — the user guide
- `docs/porting-guide.md` — bringing pushraker to another printer
- `tests/` — `python tests/test_pushraker.py` (the component against a
  stand-in Moonraker; also runs `test_settings.py`)
- `tests/test_install.py` — install.py against a fake `/proc` (also run by
  `test_pushraker.py`)
- `tests/check_deps.py` — whether pushraker works with the Apprise, Pillow and
  Jinja2 of a printer's Python, without sending anything: copy it and the
  package to the printer and run `<Moonraker's python> check_deps.py <dir
  holding pushraker/>`

## License

GPL-3.0-or-later.
