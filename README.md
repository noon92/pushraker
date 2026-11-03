# pushraker

Push notifications for Klipper printers, as a Moonraker component: print
started, paused, resumed, complete, cancelled, progress, first layer, faults
and shutdowns, power-loss recovery, chamber temperature, filament (Spoolman,
backup spools, MMUs), with pictures (camera snapshot, model preview, spool
drawings). Sent through [Apprise](https://github.com/caronc/apprise): ntfy,
Pushover, Gotify, Telegram, Discord, a webhook, email. Configured from its
own web page, live, with no Moonraker or Klipper restart.

It grew out of the Push Notifications feature of the
[Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware),
where it is installed with one setting; the user documentation lives there
for now: [notifications](https://snapmakeru1-extended-firmware.pages.dev/notifications).

**Status: 0.1.0, first release as a package.** The component has been in
use on the Snapmaker U1 for a while; the page, the settings file and the
installer are new. Other printers: the generic parts work (Klipper's
standard objects: print_stats, exclude_object, heater_generic, tmc
drivers, Moonraker's webcams and Spoolman); printer-specific parts are
behind `printers/`, one module per printer; `snapmaker_u1.py` is the
first.

## Install (any Moonraker printer)

```sh
pip install pushraker            # into the Python Moonraker runs with
python -m pushraker install      # ~/moonraker and ~/printer_data by default
```

`install` puts a one-file shim into Moonraker's `components/` folder, adds
`[push_notifications]` to `moonraker.conf`, includes the Klipper macro
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
through the `/server/` proxy of that nginx (or Moonraker's own port).

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
- Moonraker endpoints: `GET/POST /server/push_notifications/config`,
  `POST /server/push_notifications/test`,
  `GET /server/push_notifications/status`.

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
- `tests/` — `python tests/test_pushraker.py` (the component against a
  stand-in Moonraker; also runs `test_settings.py`)

## License

GPL-3.0-or-later.
