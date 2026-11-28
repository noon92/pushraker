# pushraker

Push notifications for 3D printers running Klipper and Moonraker: print
started, paused, resumed, complete, cancelled, progress, first layer,
errors and shutdowns, with pictures (camera snapshot, model preview), and
on the Snapmaker U1 also power-loss recovery, chamber temperature and
filament (Spoolman, backup spools, MMUs). Every notification's title,
contents and image can be edited on the settings page, as Jinja templates
over Klipper's values and pushraker's own (print job, toolheads, filament,
queue). Sent through [Apprise](https://github.com/caronc/apprise): ntfy,
Pushover, Gotify, Telegram, Discord, a webhook, email. Configured from its
own web page; changes apply at once, with no Moonraker or Klipper restart.

It grew out of the Push Notifications feature of the
[Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware),
where it is built in.

**Documentation:**
- [User guide](docs/guide.md): setting it up, every notification, templates, backups.
- [Template values](docs/values.md), and [which printer has which](docs/values-by-printer.md).
- [Notification files](docs/notification-files.md): one notification in a
  file, to share or import, with a
  [prompt to paste into an AI assistant](docs/notification-files.md#writing-one-with-an-ai-assistant)
  so it writes one for you.
- [Porting guide](docs/porting-guide.md): bringing pushraker to another printer.

## Supported printers

pushraker has been tested on two printers:

| Printer | How it's installed | Settings page |
|---|---|---|
| Snapmaker U1 | Built into the [Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware) | `http://<printer>/notifications/` |
| Snapmaker U1, stock firmware | With [Bespok3d](https://github.com/Bespok3d) ([below](#install-with-bespok3d-snapmaker-u1-on-stock-firmware)) | `http://<printer>/server/pushraker/page` |
| Creality Hi | Stock firmware; one command over SSH ([below](#install-on-the-creality-hi)) | `http://<printer>:7125/server/pushraker/page` |

**Other printers are untested.** The same package runs on every printer;
what differs between printers is kept in a printer profile
(`src/pushraker/printers/`). A printer without a profile gets the generic
one, written for stock Klipper but not yet tested on any printer. To bring
pushraker to another printer, see the [porting guide](docs/porting-guide.md).

### Features by printer

| Feature | Snapmaker U1 | Creality Hi |
|---|---|---|
| Print started/paused/resumed/complete/cancelled, progress, status reports, errors, Klipper shutdown, notifications from macros | ✅ | ✅ |
| Camera picture | ✅ | ✅ from the printer's own camera |
| Live Camera link | ✅ | ✅ |
| Model preview, time left and ETA | ✅ | ✅ |
| Layers (first layer, layer complete, upcoming pause) | ✅ | ✅ |
| Pre-print calibrations | ✅ listed in *Print started*, reported when done | ✅ reported when done |
| Bed cooled | ✅ | ✅ |
| Chamber temperature, driver temperature | ✅ | ❌ no such hardware |
| Filament: toolhead map, backup spools, MMU slots | ✅ | ❌ |
| Filament: Spoolman (not enough, running low) | ✅ | ➖ with Spoolman |
| Coded warnings, touchscreen calibrations, power-loss recovery | ✅ | ❌ |
| Print-end and cancel snapshots | ✅ | ❌ ¹ |
| Per-object notifications, excluded objects | ✅ | ✅ |
| Printer URL filled in on first setup | ✅ its web UI | ✅ Fluidd (port 4408) |

¹ *Print complete* and *Print cancelled* are still sent; the picture is
taken after the printer has parked, not before.

Full chart: [Features by printer](docs/guide.md#features-by-printer) in the guide.

**Status: 0.4.2.** pushraker has been in use on the Snapmaker U1 since its
first release; Creality Hi support is new in 0.4. Notifications a printer
can't send are off and left off its settings page; one can still be
turned on in `pushraker.conf`.

## Requirements

What pushraker needs from the printer (the installer checks these):

- Moonraker with its `job_state` component (in Moonraker since November
  2021). The Creality Hi's Moonraker, from 2022–23, has it.
- The Python Moonraker runs with: 3.7 or newer (3.9 or newer for
  `pip install`), with apprise 1.2 or newer, Jinja2 3.0 or newer and
  requests. Pillow 8.2 or newer is optional with the SSH installer (pip
  installs it); without it notifications go without spool pictures and
  model previews.

## Install with Bespok3d (Snapmaker U1 on stock firmware)

[Bespok3d](https://github.com/Bespok3d) adds plugins to a printer that
keeps its stock firmware: nothing is flashed. pushraker is packaged for it
and has been tested on a Snapmaker U1.

1. Set up the Bespok3d app with your printer, and turn on **Advanced
   mode** on the printer's screen (Settings > Maintenance > Advanced Mode).
2. Download `pushraker-<version>.b3` from the
   [latest release](https://github.com/noon92/pushraker/releases/latest)
   and drop it on the Bespok3d app window. Install it with the printer
   idle. It isn't in the Bespok3d store yet and isn't signed, so the app
   shows it as coming from an unknown publisher.
3. Open **Notifications** from the printer's links in the app, or
   `http://<printer>/server/pushraker/page`, and set it up.

The print-end and cancel pictures are taken before the printer parks, as
on the extended firmware: a small Klipper extra in the package runs
pushraker's macros at the start of `PRINT_END` and `CANCEL_PRINT`, without
changing the printer's own macros. Uninstalling from the app removes
everything except your settings (`pushraker.conf`). After a firmware
update, press **Recover** in the app. The package's source is in
[`bespok3d/`](bespok3d/).

## Install on the Creality Hi

Turn on root access in the printer's settings, log in over SSH as `root`,
and run:

```sh
python3 -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/noon92/pushraker/main/install.py').read())"
```

The installer (`install.py`):

- finds the running Moonraker and checks the requirements above;
- never adds, upgrades or removes any of the printer's own Python packages;
- downloads pushraker from PyPI (checked against its sha256) into
  `/mnt/UDISK/pushraker-install` (it never deletes a folder it didn't make);
- adds one file, `/usr/share/moonraker/components/pushraker.py`, and a
  `[pushraker]` section to `/usr/share/moonraker/moonraker.conf` (backed up
  first as `moonraker.conf.bak-pushraker`);
- restarts Moonraker, never while printing, and checks pushraker loaded;
- prints the settings page's address,
  `http://<printer>:7125/server/pushraker/page`, served by Moonraker
  itself: no web server change is needed.

Settings are kept in `/mnt/UDISK/printer_data/config/pushraker.conf`.
Klipper's configuration and Creality's macros are left alone.

**After a firmware update**, or **to update pushraker**, run the same
command again; the settings are kept.

**Options** go after the command (`python3 -c "..." --uninstall`):
`--uninstall` removes pushraker (also when Moonraker no longer starts;
`--purge` also removes the settings), `--version X`, `--wheel FILE`,
`--name`, `--no-restart`. `python3 install.py --help` lists them all.

On the Hi, pushraker works with Creality's older Moonraker and Apprise,
reads the model preview, estimate and filament from the G-code files
themselves, takes layers from Creality's own count, and takes camera
pictures from the Hi's camera through the firmware's ffmpeg. See
[Creality Hi](docs/guide.md#creality-hi) in the guide.

The installer is written to find Moonraker on other printers whose
Moonraker came with the firmware too, but it has only been tested on the
Creality Hi.

## Install with pip (untested)

Not yet tested on a printer. For a printer where you installed Moonraker
yourself:

```sh
pip install pushraker            # into the Python Moonraker runs with
python -m pushraker install      # ~/moonraker and ~/printer_data by default
```

`install` puts a one-file shim into Moonraker's `components/` folder, adds
`[pushraker]` to `moonraker.conf`, and includes the Klipper macro file
from `printer.cfg` (`--no-klipper` skips it). Restart Moonraker and
Klipper; pushraker writes `printer_data/config/pushraker.conf` at its
first start. The settings page is served by Moonraker at
`/server/pushraker/page` on Moonraker's port (7125 unless `moonraker.conf`
sets another). Optionally, serve the page from the nginx that serves
Fluidd or Mainsail too (`src/pushraker/web/index.html`, or `python -c
"import pushraker, os; print(os.path.dirname(pushraker.__file__) +
'/web')"`), e.g.

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
