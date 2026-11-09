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
[Push Notifications guide](docs/guide.md).

**Status: 0.3.** The component has been in
use on the Snapmaker U1 for a while; the page, the settings file and the
installer are new. Other printers: the generic parts work (Klipper's
standard objects: print_stats, exclude_object, heater_generic, tmc
drivers, Moonraker's webcams and Spoolman); printer-specific parts are
behind `printers/`, one module per printer; `snapmaker_u1.py` is the
first. Notifications a printer can't produce (the U1's touchscreen
calibrations, coded warnings, power-loss recovery, toolhead-map refills,
MMU slots) start off on other printers and are left off the page; turn
one on in `pushraker.conf` and it shows on the page and is sent like any
other.

## Install (any Moonraker printer)

```sh
pip install pushraker            # into the Python Moonraker runs with
python -m pushraker install      # ~/moonraker and ~/printer_data by default
```

`install` puts a one-file shim into Moonraker's `components/` folder, adds
`[pushraker]` to `moonraker.conf`, includes the Klipper macro
file from `printer.cfg`, and creates `printer_data/config/pushraker.conf`.
Restart Moonraker and Klipper. Then serve the settings page
(`src/pushraker/web/index.html`, or `python -c "import pushraker, os;
print(os.path.dirname(pushraker.__file__) + '/web')"`) from the same nginx
that serves Fluidd or Mainsail, e.g.

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
- Moonraker endpoints: `GET/POST /server/pushraker/config`,
  `POST /server/pushraker/test`, `GET /server/pushraker/status` (also
  under `/server/push_notifications/`, the 0.1 names).

## Layout

- `src/pushraker/component.py` — the Moonraker component
- `src/pushraker/settings.py` — the settings: validation, storage, the
  Apprise URLs built from them
- `src/pushraker/printers/` — what differs between printers (the harness
  side of the package), one module per printer; `snapmaker_u1.py` is the
  first
- `src/pushraker/images.py` — spool pictures (Pillow)
- `src/pushraker/email_html.py`, `src/pushraker/discord.py` — email card,
  Discord senders
- `src/pushraker/web/index.html` — the settings page
- `src/pushraker/klipper/pushraker.cfg` — the Klipper macro file
- `src/pushraker/__main__.py` — `python -m pushraker install|uninstall|status`
- `docs/guide.md` — the user guide
- `tests/` — `python tests/test_pushraker.py` (the component against a
  stand-in Moonraker; also runs `test_settings.py`)

## License

GPL-3.0-or-later.
