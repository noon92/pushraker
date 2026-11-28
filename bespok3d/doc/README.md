# pushraker

Push and email notifications for your print jobs, with a camera picture or the model preview.

- **Print jobs:** started, first layer, layers, 25 / 50 / 75 %, paused, upcoming pause, resumed,
  complete, cancelled, bed cooled.
- **Faults:** errors and warnings with the same text as the printer's screen, Klipper shutdown,
  power loss recovery.
- **Filament and calibrations:** not enough filament, spool running low, switched to backup spool
  (with Spoolman), and the calibrations you start from the screen.
- **Services:** ntfy, Pushover, Gotify, Telegram, Discord, a webhook, email.

## Customizable

Every notification's title and text are templates you can edit, with a live preview and a list of
every value they can use. Write your own notifications too: sent when a condition you set is true
(checked every few seconds, or at each layer, tool change or console message), once per print or
every time, or straight from a macro with `RESPOND PREFIX=notify_custom MSG=pushraker.<name>`.

## Setting it up

After installing, open **Notifications** from the printer's links in the app, or
`http://<printer>/server/pushraker/page`. Nothing is sent until you turn notifications on and set
up a service. Tokens and passwords stay on the printer.

## Pictures at the end of a print

The printer's own `PRINT_END` and `CANCEL_PRINT` park the toolhead and lower the bed. To take the
picture before that, this plugin runs pushraker's `_PRINT_END_NTFY` and `_CANCEL_PRINT_NTFY`
macros first: they lift the toolhead a little and report the event, then the printer's own macro
carries on. The printer's macros themselves are not changed.

## Good to know

- On the Snapmaker U1, turn on **Advanced mode** on the printer's screen (Settings > Maintenance >
  Advanced Mode). With it off, every reboot drops the lines that load this plugin.
- Your settings live in `pushraker.conf` beside `moonraker.conf`, so they survive an update or an
  uninstall and reinstall.

Full guide: https://github.com/noon92/pushraker/blob/main/docs/guide.md
