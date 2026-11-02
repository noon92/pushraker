# pushraker

Push notifications for Klipper printers, as a Moonraker component: print
started, paused, resumed, complete, cancelled, progress, first layer, faults
and shutdowns, power-loss recovery, chamber temperature, filament (Spoolman
and MMU backup spools), with pictures (camera snapshot, model preview, spool
drawings). Sent through [Apprise](https://github.com/caronc/apprise): ntfy,
Pushover, Gotify, Telegram, Discord, a webhook, email.

Grew out of the Push Notifications feature of the
[Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware);
it is being split out so it works on any Moonraker printer and is configured
from its own web page, live, without editing `moonraker.conf`.

**Status: work in progress — not yet usable on its own.** The component is
here and tested; the settings API, the web page and the installer are next.

## Layout

- `src/pushraker/component.py` — the Moonraker component
- `src/pushraker/images.py` — spool pictures (Pillow)
- `src/pushraker/email_html.py`, `src/pushraker/discord.py` — email card, Discord plugin
- `shims/push_notifications.py` — the one file that goes into Moonraker's
  `components/` directory to load the package
- `tests/test_pushraker.py` — the test suite (`python tests/test_pushraker.py`)

## License

GPL-3.0-or-later.
