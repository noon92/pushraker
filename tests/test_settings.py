# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""pushraker.settings: validation, the Apprise URLs, the store, the move
from the extended firmware's INI settings. Run: python tests/test_settings.py
(also run by test_pushraker.py)."""

import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import pushraker.settings as st  # noqa: E402

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


ENV = st.Env(cameras={"case": {"snapshot": "http://127.0.0.1/webcam/snapshot.jpg",
                               "stream": "/webcam/webrtc"}},
             spoolman_url="http://spoolman:7912",
             error_index_url="https://wiki.example.com/errors", name="U1")


def fresh():
    return st.defaults("U1")


def bad(data, what, env=ENV):
    try:
        st.validate(dict(enabled=True, **data), fresh(), env)
        check(what, False, "accepted")
    except st.ConfigError:
        check(what, True)


def run_settings_tests():
    print("pushraker.settings")
    bad(dict(provider="ntfy", server="ftp://x", topic="u1"),
        "rejects a server that isn't http(s)")
    bad(dict(provider="ntfy", server="https://ntfy.sh", topic="short"),
        "rejects a short topic on the public server")
    bad(dict(provider="telegram", secret="nope"), "rejects a bad bot token")
    bad(dict(provider="gotify", gotify_server="ftp://x", secret="A" * 15),
        "Gotify: rejects a server that isn't http(s)")
    bad(dict(provider="gotify", gotify_server="https://g.example.com:99999",
             secret="A" * 15), "Gotify: rejects a port out of range")
    bad(dict(provider="gotify", gotify_server="https://g.example.com",
             secret="no"), "Gotify: rejects a bad app token")
    g = st.validate(dict(enabled=True, provider="gotify",
                         gotify_server="https://g.example.com:8443/gotify/",
                         secret="AbC.dEf-123_xyz", printer_url="http://u1",
                         camera="case"), fresh(), ENV)
    gurl = st.row_url(g, "error", "max", ENV)
    check("Gotify: URL with port, path, priority and Markdown",
          gurl == "gotifys://g.example.com:8443/gotify/AbC.dEf-123_xyz"
                  "?priority=emergency&format=markdown", gurl)
    gopts = st.render_options(g, ENV)
    check("Gotify: no pictures, links in the text",
          gopts["error"].startswith("none, gotifys://g.example.com:8443/")
          and gopts["web_ui_url"] == "http://u1/"
          and gopts["camera_url"] == "http://u1/webcam/webrtc", gopts)
    bad(dict(provider="telegram", secret="12345:" + "A" * 35,
             telegram_chat="123", printer_url="http://u1"),
        "rejects a dot-less printer host for Telegram (links are dropped)")
    st.validate(dict(provider="telegram", secret="12345:" + "A" * 35,
                     telegram_chat="123", printer_url="http://100.64.0.10"),
                fresh(), ENV)
    st.validate(dict(provider="ntfy", server="https://ntfy.example.com",
                     topic="u1", printer_url="http://u1"), fresh(), ENV)
    check("a dot-less printer host is fine for the other services", True)
    ts = st.Env(cameras=ENV.cameras, tailscale=("u1", "100.64.0.10"))
    tg = dict(enabled=True, provider="telegram", secret="12345:" + "A" * 35,
              telegram_chat="123", printer_url="http://U1/", camera="case")
    s = st.validate(tg, fresh(), ts)
    check("Telegram: the Tailscale name is accepted and sent as the IP",
          st.links(s, "test", ts)[:2] == [
              ("Web UI", "http://100.64.0.10/"),
              ("Live Camera", "http://100.64.0.10/webcam/webrtc")]
          and st.render_options(s, ts)["web_ui_url"] == "http://100.64.0.10/"
          and s["printer_url"] == "http://U1/", st.links(s, "test", ts))
    bad(dict(provider="telegram", secret="12345:" + "A" * 35,
             telegram_chat="123", printer_url="http://other"),
        "Telegram: another dot-less name is still refused", ts)
    s = st.validate(dict(enabled=True, provider="ntfy",
                         server="https://ntfy.example.com", topic="u1",
                         printer_url="http://u1"), fresh(), ts)
    check("other services keep the Tailscale name",
          st.links(s, "test", ts)[0] == ("Web UI", "http://u1/"))
    bad(dict(provider="discord", secret="https://example.com/x"),
        "rejects a URL that isn't a Discord webhook")

    s = st.validate(dict(enabled=True, provider="ntfy",
                         server="https://ntfy.example.com", topic="u1",
                         printer_url="http://u1", camera="case"), fresh(), ENV)
    opts = st.render_options(s, ENV)
    check("options list the enabled notifications",
          opts["started"].startswith("render, ntfys://ntfy.example.com/u1"),
          opts.get("started"))
    pp = st.Env(cameras=ENV.cameras, preprint=True)
    check("pre-print calibrations complete: Print started's destination, "
          "on printers that have them, while its checkbox is on",
          "preprint_done" not in opts
          and st.render_options(s, pp).get("preprint_done", "").startswith(
              "render, ntfys://ntfy.example.com/u1")
          and "preprint_done" not in st.render_options(
              dict(s, preprint_done=False), pp))
    comp = open(os.path.join(HERE, "..", "src", "pushraker",
                             "component.py")).read()
    known = set(re.findall(r'(?:get|num)\(\s*"(\w+)"', comp))
    rows = {k for k in opts if k.split("_email")[0].split("_webhook")[0]
            in st.ROW_IDS + [st.OBJECT_ROW, st.FILE_PAUSE_ROW]
            or k.startswith("macro_")}
    check("every option is one the component reads",
          set(opts) - rows <= known, sorted(set(opts) - rows - known))
    check("a file's pause: the Paused row's destinations at priority max",
          opts["paused"].startswith("snapshot, ntfys://")
          and opts["file_pause"].startswith("snapshot, ntfys://")
          and "priority=max" in opts["file_pause"], opts.get("file_pause"))
    check("print-by-object: object complete follows the Print complete row",
          opts["object_complete"].startswith("snapshot, ntfys://")
          and opts["first_layer_objects"] == "each"
          and "complete_objects" not in opts)
    s["complete_objects"] = "once"
    check("or is left out when complete is once per print job",
          "object_complete" not in st.render_options(s, ENV))
    s["complete_objects"] = "each"
    check("the error index link comes from the environment (the harness)",
          "Error%20Index" in opts["error"] and "wiki.example.com" in opts["error"]
          and "Error%20Index" not in st.render_options(
              s, st.Env(cameras=ENV.cameras)).get("error", ""), opts["error"])
    bad(dict(provider="ntfy", server="https://ntfy.example.com", topic="u1",
             first_layer_objects="sometimes"),
        "rejects a bad print-by-object setting")
    check("ntfy buttons from the printer URL",
          "view%2C%20Web%20UI%2C%20http%3A%2F%2Fu1%2F" in opts["started"])
    check("no camera: snapshots become no image, no snapshot URL",
          "snapshot_url" not in st.render_options(
              dict(s, camera=""), ENV)
          and st.render_options(dict(s, camera=""), ENV)["paused"]
          .startswith("none, "))
    check("an unknown camera name is the same as none",
          "snapshot_url" not in st.render_options(dict(s, camera="nope"), ENV))
    check("the old camera names map to Moonraker's",
          st.validate(dict(camera="internal"), fresh(), ENV)["camera"] == "case")
    hv = st.hook_values(s, ENV)
    check("print hooks: lift and report for complete and cancel, the error "
          "row for a fault cancel", hv == {"complete": 2, "cancelled": 2,
                                           "fault": 2}, hv)
    s2 = dict(s, rows=dict(s["rows"], complete=dict(s["rows"]["complete"],
                                                     image=False)))
    s2["rows"]["error"] = dict(s2["rows"]["error"], enabled=False)
    hv = st.hook_values(s2, ENV)
    check("...report only without a picture; cancel row when error is off",
          hv == {"complete": 1, "cancelled": 2, "fault": 2}, hv)
    off = dict(s, enabled=False)
    check("nothing enabled: hooks do nothing",
          st.hook_values(off, ENV) == {"complete": 0, "cancelled": 0,
                                       "fault": 0}
          and st.render_options(off, ENV) == {})

    tok = "12345:" + "TESTTOKEN" + "x" * 26   # format only; GitHub wants 8+ digits
    t = st.validate(dict(enabled=True, provider="telegram", secret=tok,
                         telegram_chat="555"), fresh(), ENV)
    url = st.row_url(t, "progress_25", "low", ENV)
    check("Telegram: HTML, no Apprise icon, low priority is silent",
          url.startswith(f"tgram://{tok}/555?") and "format=html" in url
          and "image=no" in url and "silent=yes" in url, url)
    hook = ("https://discord.com/api/webhooks/1234567890123/"
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abc")
    d = st.validate(dict(enabled=True, provider="discord", secret=hook,
                         discord_mention="everyone"), fresh(), ENV)
    check("Discord: mention only on high/max",
          "ping=everyone" in st.row_url(d, "error", "max", ENV)
          and "ping" not in st.row_url(d, "complete", "default", ENV))
    w = st.validate(dict(enabled=True, provider="ntfy",
                         server="https://ntfy.example.com", topic="u1",
                         webhook_enabled=True,
                         webhook_url="https://u:p@hooks.example.com/x/abc?k=1",
                         printer_url="http://u1", camera="case"), fresh(), ENV)
    w["rows"]["progress_25"]["enabled"] = False
    wopts = st.render_options(w, ENV)
    wurl = st.webhook_url(w, "complete", ENV)
    check("webhook: JSON sender, own query kept, event, printer, links",
          wurl.startswith("jsons://u:p@hooks.example.com/x/abc?")
          and "-k=1" in wurl and ":event=complete" in wurl
          and ":printer=U1" in wurl and ":web_ui=http%3A%2F%2Fu1%2F" in wurl,
          wurl)
    check("webhook is added alongside the push service",
          wopts["complete"].startswith("snapshot, ntfys://")
          and wopts["complete_webhook"].startswith("snapshot, jsons://"))
    check("webhook gets notifications that push and email don't",
          "progress_25_webhook" in wopts and "progress_25" not in wopts)
    hook_only = st.validate(dict(enabled=True, push_enabled=False,
                                 webhook_enabled=True,
                                 webhook_url="http://hooks.local/x"),
                            fresh(), ENV)
    check("a webhook alone is enough",
          st.configured(hook_only)
          and st.hook_values(hook_only, ENV)["complete"] == 1)
    bad(dict(push_enabled=False, webhook_enabled=True,
             webhook_url="ftp://example.com/x"),
        "rejects a webhook that isn't http(s)")
    bad(dict(push_enabled=False), "rejects nothing enabled at all")
    check("push off: no push lines, and the provider's fields aren't checked",
          "started" not in st.render_options(st.validate(
              dict(enabled=True, push_enabled=False, provider="ntfy",
                   server="", email_enabled=True, email_host="smtp.x.com",
                   email_from="a@x.com", email_to="b@x.com"), fresh(), ENV),
              ENV))
    sp = st.validate(dict(enabled=True, provider="ntfy",
                          server="https://ntfy.example.com", topic="u1"),
                     fresh(), ENV)
    no_sp = st.Env(cameras=ENV.cameras)
    check("Spoolman rows only with Spoolman",
          "filament_short" in st.render_options(sp, ENV)
          and "filament_short" not in st.render_options(sp, no_sp)
          and "spool_switch" in st.render_options(sp, no_sp))

    # The store: pushraker.conf (no secrets) + pushraker.secrets (0600)
    work = tempfile.mkdtemp(prefix="pushraker_settings_")
    cfgdir = os.path.join(work, "config")
    store = st.Store(cfgdir, work, name="U1")
    check("no file yet: defaults", not store.exists()
          and store.load()["name"] == "U1" and not store.load()["enabled"])
    tok = st.validate(dict(enabled=True, provider="ntfy",
                           server="https://ntfy.example.com", topic="u1",
                           auth="token", secret="tk_secret123"), fresh(), ENV)
    store.save(tok)
    cfg_text = open(store.settings_path).read()
    sec_text = open(store.secrets_path).read()
    check("pushraker.conf holds no secret; the secrets file does, mode 0600",
          "tk_secret123" not in cfg_text and "tk_secret123" in sec_text
          and oct(os.stat(store.secrets_path).st_mode & 0o777) == "0o600"
          and cfg_text.startswith("[pushraker]\n")
          and "\nstarted: on, default, image, no_email\n" in cfg_text,
          cfg_text[:300])
    back = store.load()
    check("everything comes back", back["secret"] == "tk_secret123"
          and back["secret_for"] == "ntfy|token|https://ntfy.example.com|"
          and back["server"] == "https://ntfy.example.com"
          and back["rows"] == tok["rows"] and back["auth"] == "token",
          (back["secret_for"], back["auth"]))
    check("unchanged files: nothing changed", not store.changed())
    with open(store.settings_path, "a") as f:
        f.write("name: Edited\n")
    os.utime(store.settings_path, None)
    check("a hand edit is noticed and read",
          store.changed() and store.load()["name"] == "Edited")
    sw = st.validate(dict(enabled=False, push_enabled=False,
                          provider="pushover"), back, ENV)
    check("switching services drops the stored secret (never replayed to "
          "another host)", sw["secret"] == "" and not sw["secret_for"])
    try:
        st.validate(dict(provider="pushover", pushover_user="u" * 30), back,
                    ENV)
        check("...and asks for the new service's own token", False)
    except st.ConfigError as e:
        check("...and asks for the new service's own token",
              "another service or server" in str(e), str(e))
    try:
        st.validate(dict(server="https://other.example.com"), back, ENV)
        check("so does another ntfy server", False)
    except st.ConfigError as e:
        check("so does another ntfy server",
              "another service or server" in str(e), str(e))
    same = st.validate(dict(topic="u2"), back, ENV)
    check("the same service keeps it", same["secret"] == "tk_secret123")
    mail = dict(enabled=True, provider="ntfy", auth="none",
                server="https://ntfy.example.com", topic="u1",
                email_enabled=True, email_host="smtp.gmail.com",
                email_port=587, email_security="starttls",
                email_username="me@gmail.com", email_password="app-pass",
                email_from="me@gmail.com", email_to="me@gmail.com")
    m = st.validate(mail, fresh(), ENV)
    store.save(m)
    check("email password saved with its scope",
          store.load()["email_password_for"]
          == "smtp.gmail.com|587|starttls|me@gmail.com")
    for change in (dict(email_host="smtp.evil.example"),
                   dict(email_security="none"),
                   dict(email_username="other@gmail.com")):
        try:
            st.validate(dict(mail, email_password="", **change),
                        store.load(), ENV)
            check(f"another {list(change)[0]} never gets the saved password",
                  False)
        except st.ConfigError as e:
            check(f"another {list(change)[0]} never gets the saved password",
                  "another server or account" in str(e), str(e))
    st.validate(dict(mail, email_password="", email_to="you@gmail.com"),
                store.load(), ENV)
    check("the same server and account keeps it", True)
    st.validate(dict(mail, email_password="new-pass",
                     email_host="smtp.other.example"), store.load(), ENV)
    check("a new password for another host is fine", True)
    store.save(dict(m, enabled=False, secret="", email_password="",
                    email_username=""))
    check("no secrets left: the secrets file is removed",
          not os.path.exists(store.secrets_path))

    # The move from the extended firmware's settings
    ext = os.path.join(work, "extended2.cfg")
    with open(ext, "w") as f:
        f.write("[camera]\ninternal: paxx12\n\n[notifications]\nenabled: true\n"
                "provider: ntfy\nserver: https://ntfy.example.com\ntopic: u1\n"
                "auth: token\ncamera: internal\nname: Martha\n"
                "started: off, low, no_image, email\n"
                "complete: on, high, image, no_email\n")
    secrets = os.path.join(work, "moonraker.secrets")
    with open(secrets, "w") as f:
        f.write("[notifications]\nsecret = tk_old\n"
                "secret_for = ntfy|token|https://ntfy.example.com|\n"
                "push_token = tk_old\n")
    moved = st.import_ini(ext, "notifications", secrets, "notifications",
                          name="U1")
    check("extended2.cfg [notifications] + moonraker.secrets are read",
          moved["enabled"] and moved["name"] == "Martha"
          and moved["secret"] == "tk_old" and moved["camera"] == "case"
          and moved["rows"]["started"] == {"enabled": False, "priority": "low",
                                           "image": False, "email": True}
          and moved["rows"]["complete"]["priority"] == "high", moved)
    check("...and pass validation as they are",
          not st.stored_error(moved, ENV), st.stored_error(moved, ENV))
    check("older settings: provider none means push off",
          not st.parse_section({"provider": "none"}, fresh())["push_enabled"])
    view = st.public_view(moved, ENV, False, "0.1.0")
    check("the page never sees a secret",
          view["secret"] == "" and view["has_secret"]
          and "secret_for" not in view and view["meta"]["cameras"]["case"]
          == {"snapshot": True, "stream": True}
          and view["meta"]["summary"] == "Enabled: ntfy", view["meta"])
    return _failures


def run_hidden_rows_tests():
    """Rows a printer can't produce: off by default and left off the page,
    unless turned on in pushraker.conf; a save from the page keeps them."""
    print("rows a printer can't produce")
    gone = ("calibration", "warning", "power_loss")
    env = st.Env(cameras=ENV.cameras, rows_unavailable=gone)
    d = st.defaults("Hi", gone)
    check("they start off (push and email) on that printer",
          all(not d["rows"][r]["enabled"] and not d["rows"][r]["email"]
              for r in gone) and d["rows"]["error"]["enabled"],
          {r: d["rows"][r] for r in gone})
    check("...and as usual elsewhere",
          st.defaults("U1")["rows"]["warning"]["enabled"])
    meta = {m["id"]: m for m in st.public_view(d, env, False, "0")["meta"]["rows"]}
    check("off and unavailable: hidden on the page",
          all(meta[r]["hidden"] for r in gone) and not meta["error"]["hidden"])
    work = tempfile.mkdtemp(prefix="pushraker_hidden_")
    store = st.Store(os.path.join(work, "config"), work, name="Hi")
    store.unavailable = gone
    store.save(store.load())
    text = open(store.settings_path).read()
    check("the settings file lists them, off",
          "\nwarning: off, max, image, no_email\n" in text, text[-400:])
    with open(store.settings_path, "w") as f:
        f.write(text.replace("\nwarning: off, max, image, no_email\n",
                             "\nwarning: on, max, image, no_email\n"))
    edited = store.load()
    meta = {m["id"]: m for m in st.public_view(edited, env, False, "0")
            ["meta"]["rows"]}
    check("turned on by hand: shown on the page",
          not meta["warning"]["hidden"] and meta["warning"]["available"] is False
          and meta["calibration"]["hidden"])
    # The page sends only the rows it shows; a hidden one keeps its setting
    edited["rows"]["calibration"]["email"] = False
    page_rows = {rid: dict(edited["rows"][rid]) for rid in st.ROW_IDS
                 if rid not in ("calibration", "power_loss")}
    page_rows["error"]["priority"] = "high"
    saved = st.validate(dict(enabled=True, provider="ntfy",
                             server="https://ntfy.example.com", topic="hi",
                             rows=page_rows), edited, env)
    store.save(saved)
    back = store.load()
    check("a save from the page keeps the rows it doesn't show, and the "
          "hand-enabled one",
          back["rows"]["warning"]["enabled"]
          and not back["rows"]["calibration"]["enabled"]
          and back["rows"]["error"]["priority"] == "high", back["rows"]["warning"])
    check("...and the hand-enabled row is sent like any other",
          "warning" in st.render_options(back, env))
    return _failures


def run_installer_tests():
    """python -m pushraker install/uninstall on printer.cfg and moonraker.conf."""
    print("pushraker installer")
    import pushraker.__main__ as cli
    work = tempfile.mkdtemp(prefix="pushraker_install_")
    cfg = os.path.join(work, "printer.cfg")
    saved = ("[printer]\nkinematics: corexy\n\n"
             "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
             "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
             "#*#\n#*# [probe]\n#*# z_offset = 1.234\n")
    with open(cfg, "w") as f:
        f.write(saved)
    cli.ensure_section(cfg, "include pushraker.cfg", "[include pushraker.cfg]\n", "")
    text = open(cfg).read()
    check("the include goes before Klipper's SAVE_CONFIG block",
          text.index("[include pushraker.cfg]") < text.index("#*# <---")
          and text.endswith("#*# z_offset = 1.234\n")
          and "\n\n[include pushraker.cfg]\n\n#*# <---" in text, text)
    check("...and only once", not cli.ensure_section(
        cfg, "include pushraker.cfg", "[include pushraker.cfg]\n", ""))
    cli.drop_section(cfg, "include pushraker.cfg", "")
    text = open(cfg).read()
    check("uninstall removes just that line; the saved calibration stays",
          "[include" not in text and "#*# z_offset = 1.234" in text
          and "[printer]\nkinematics: corexy" in text, text)
    moon = os.path.join(work, "moonraker.conf")
    with open(moon, "w") as f:
        f.write("[server]\nhost: 0.0.0.0\n")
    cli.ensure_section(moon, "push_notifications", "[push_notifications]\nname: X\n", "")
    cli.ensure_section(moon, "push_notifications", "[push_notifications]\nname: Y\n", "")
    cli.drop_section(moon, "push_notifications", "")
    check("a section is added once and removed whole",
          open(moon).read() == "[server]\nhost: 0.0.0.0\n\n",
          repr(open(moon).read()))
    # The import of older settings: provider none, bad printer URL
    check("provider none (older settings) becomes push off with a valid provider",
          st.parse_section({"provider": "none"}, fresh())["provider"] == "ntfy")
    for url in ("http://u1:99999", "http://[::1"):
        try:
            st.validate(dict(enabled=True, provider="telegram",
                             secret="12345:" + "A" * 35, telegram_chat="1",
                             printer_url=url), fresh(), ENV)
            check(f"a printer URL that urlparse chokes on ({url}) is refused", False)
        except st.ConfigError:
            check(f"a printer URL that urlparse chokes on ({url}) is refused", True)
        except ValueError as e:
            check(f"a printer URL that urlparse chokes on ({url}) is refused", False,
                  f"ValueError {e}")
    # 0.1.x installed [push_notifications] and push_notifications.py
    import contextlib
    import io as _io
    root = tempfile.mkdtemp(prefix="pushraker_rename_")
    comps = os.path.join(root, "moonraker", "moonraker", "components")
    os.makedirs(comps)
    cfgd = os.path.join(root, "printer_data", "config")
    os.makedirs(cfgd)
    open(os.path.join(comps, "push_notifications.py"), "w").write("# old\n")
    with open(os.path.join(cfgd, "moonraker.conf"), "w") as f:
        f.write("[server]\nhost: 0.0.0.0\n\n[push_notifications]\nname: Old\n")
    open(os.path.join(cfgd, "printer.cfg"), "w").write("[printer]\n")
    with contextlib.redirect_stdout(_io.StringIO()):
        cli.main(["install", "--moonraker-dir", os.path.join(root, "moonraker"),
                  "--data-dir", os.path.join(root, "printer_data"), "--name", "X"])
    moon = open(os.path.join(cfgd, "moonraker.conf")).read()
    check("install: [pushraker] and pushraker.py replace 0.1.x's names",
          "[pushraker]" in moon and "[push_notifications]" not in moon
          and os.path.exists(os.path.join(comps, "pushraker.py"))
          and not os.path.exists(os.path.join(comps, "push_notifications.py")),
          moon)
    return _failures


if __name__ == "__main__":
    run_settings_tests()
    run_hidden_rows_tests()
    run_installer_tests()
    print(f"\n{_passes} passed, {_failures} failed")
    sys.exit(1 if _failures else 0)
