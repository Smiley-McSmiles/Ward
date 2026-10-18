#!/usr/bin/env python3
"""Screenshots of Ward with demo data, for the README, the AppStream metainfo and visual checks in CI.

    xvfb-run -a -s "-screen 0 1400x1000x24" python3 tools/screenshot.py OUT_DIR [scenario ...]

Each scenario runs in its own process with a fresh WARD_HOME and no network (WARD_OFFLINE=1).  Rendering uses
the cairo renderer so it works without a GPU."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS = {
    "welcome": dict(size=(900, 680)),
    "chat": dict(size=(1200, 780), open="Sam Rivera"),
    "chat-dark": dict(size=(1200, 780), open="Sam Rivera", dark=True),
    "group": dict(size=(1280, 800), open="Ward contributors"),
    "bubbles": dict(size=(1100, 760), open="Sam Rivera", layout="bubbles"),
    "compact": dict(size=(1100, 760), open="Ward contributors", layout="compact"),
    "narrow": dict(size=(400, 780), open="Sam Rivera"),
    "preferences": dict(size=(1100, 780), dialog="preferences"),
    "devices": dict(size=(1100, 780), open="Sam Rivera", dialog="devices"),
    "details": dict(size=(1100, 780), open="Ward contributors", dialog="details"),
}

NOW = int(time.time() * 1000)
MIN = 60 * 1000

CHAT = [
    (1, "Sam Rivera", -180, "Did you try the new build? The **OMEMO** setup was painless for me 🎉"),
    (0, "", -178, "Yes! Scanning the QR code verified my laptop in a second."),
    (1, "Sam Rivera", -177, "Here's the config I used:\n```toml\n[encryption]\ndefault = \"always\"\n"
                            "trust = \"btbv\"\n```"),
    (0, "", -60, "Nice. Quick list for tomorrow:\n- [x] update the server\n- [ ] invite the team\n- [ ] ~~print~~ "
                 "share the slides"),
    (1, "Sam Rivera", -58, "> invite the team\nI can do that, I'm admin on the group anyway"),
    (1, "Sam Rivera", -2, "See you at 10 then! ||it's a surprise party||"),
]
GROUP = [
    (1, "maria", -300, "Morning everyone! 👋"),
    (1, "jun", -298, "Morning! @alex did the release go out?"),
    (0, "", -290, "It did — packages for Debian, Fedora, Arch, Alpine and the BSDs are up."),
    (1, "maria", -200, "| Distro | Status |\n|---|---|\n| FreeBSD | ✅ |\n| OpenBSD | ✅ |\n| NetBSD | ✅ |"),
    (1, "jun", -100, "Great work. I'm writing the announcement now."),
    (1, "maria", -5, "😍"),
]


async def populate(home: str, scenario: dict) -> None:
    from ward.core.chats import insert_message_job
    from ward.core.core import Core
    from ward.settings import Settings

    settings = Settings()
    if scenario.get("dark"):
        settings["appearance.style"] = "dark"
    if scenario.get("layout"):
        settings["appearance.layout"] = scenario["layout"]
    settings.flush()
    if scenario is SCENARIOS["welcome"]:
        return
    core = Core(settings, connect=False)
    await core.start()
    acc = await core.add_account("alex@example.org", "x", "never", connect=False)
    await core.accounts.set_option(acc, "nickname", "Alex")
    db = core.db
    for jid, name, sub in (("sam@example.org", "Sam Rivera", "both"), ("kim@jabber.example", "Kim", "both"),
                           ("lee@example.net", "Lee", "to")):
        c = await core.contacts.ensure(acc.id, jid, in_roster=True)
        c.roster_name = name
        c.subscription = sub
        await core.contacts.save(c)
    sam = await core.open_chat(acc, "sam@example.org")
    sam.name = "Sam Rivera"
    group = await core.chats.get_or_create(acc.id, "team@groups.example.org", "groupchat", name="Ward contributors")
    kim = await core.open_chat(acc, "kim@jabber.example")
    kim.name = "Kim"
    lee = await core.open_chat(acc, "lee@example.net")
    lee.name = "Lee"
    for chat, rows in ((sam, CHAT), (group, GROUP)):
        for incoming, who, minutes, body in rows:
            values = {"chat_id": chat.id, "direction": 0 if incoming else 1, "timestamp": NOW + minutes * MIN,
                      "sender": (who if chat.type == "groupchat" else chat.jid) if incoming else
                      ("alex" if chat.type == "groupchat" else acc.jid),
                      "sender_jid": "" if chat.type == "groupchat" else (chat.jid if incoming else acc.jid),
                      "body": body, "is_markdown": 1, "kind": "text", "encryption": "omemo2",
                      "status": 3 if not incoming else 0, "trust_at_receipt": "verified" if incoming else "",
                      "msg_id": "m%d" % (minutes + 1000), "stanza_id": "s%d" % (minutes + 1000)}
            await db.run(lambda conn, v=values: insert_message_job(conn, v))
    await db.execute("INSERT INTO reactions (message_id, sender, emojis, timestamp) SELECT id, 'alex@example.org', "
                     "'[\"❤️\"]', 0 FROM messages WHERE body LIKE 'Here''s the config%'")
    await db.execute("INSERT INTO reactions (message_id, sender, emojis, timestamp) SELECT id, 'jun', "
                     "'[\"🎉\", \"👍\"]', 0 FROM messages WHERE body LIKE 'It did%'")
    for chat, minutes in ((kim, -1500), (lee, -4000)):
        chat.last_activity = NOW + minutes * MIN
    sam.update(unread=0, pinned=True, last_activity=NOW - 2 * MIN)
    group.update(unread=3, mentions=1, last_activity=NOW - 5 * MIN, subject="Building a better XMPP client",
                 member_count=3, joined=True, our_nick="alex", our_role="moderator", our_affiliation="owner",
                 encryptable=True)
    kim.update(unread=2, draft="")
    for c in (sam, group, kim, lee):
        await core.chats.save(c)
    await core.shutdown()


def run_scenario(name: str, out: str) -> int:
    home = tempfile.mkdtemp(prefix="ward-shot-")
    env = dict(os.environ, WARD_HOME=home, WARD_SECRETS="memory", WARD_OFFLINE="1", GSK_RENDERER="cairo",
               GTK_A11Y="none", NO_AT_BRIDGE="1", PYTHONPATH=ROOT, LANG=os.environ.get("LANG", "C.UTF-8"))
    return subprocess.call([sys.executable, __file__, "--child", name, out], env=env, cwd=ROOT)


def child(name: str, out: str) -> int:
    import asyncio

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk

    from ward import aio, i18n

    i18n.setup()
    scenario = SCENARIOS[name]
    loop = aio.install()
    fut = asyncio.ensure_future(populate(os.environ["WARD_HOME"], scenario), loop=loop)
    ctx = GLib.MainContext.default()
    with loop.running():
        while not fut.done():
            ctx.iteration(True)
    fut.result()

    from ward.gui.app import WardApplication

    app = WardApplication()
    status = {"code": 1}

    def shoot():
        w = app.window
        if os.environ.get("WARD_DEBUG_TREE"):
            def walk(x, depth=0):
                if type(x).__name__ == "Bin" or "Bin" in type(x).__gtype__.name:
                    chain, p = [], x.get_parent()
                    while p is not None and len(chain) < 4:
                        chain.append(type(p).__gtype__.name)
                        p = p.get_parent()
                    print(repr(x), x.get_width(), chain)
                c = x.get_first_child()
                while c is not None:
                    walk(c, depth + 1)
                    c = c.get_next_sibling()
            walk(w)
        p = Gtk.WidgetPaintable.new(w)
        width, height = w.get_width(), w.get_height()
        snap = Gtk.Snapshot()
        p.snapshot(snap, width, height)
        node = snap.to_node()
        if node is None:
            print("nothing rendered", file=sys.stderr)
            app.quit_app(force=True)
            return False
        tex = w.get_renderer().render_texture(node, None)
        tex.save_to_png(out)
        print("saved", out)
        status["code"] = 0
        app.quit_app(force=True)
        return False

    def setup():
        w = app.window
        if w is None:
            return True
        width, height = scenario["size"]
        w.set_default_size(width, height)
        target = scenario.get("open")
        if target:
            for chat in app.core.chats.items():
                if chat.name == target:
                    w.show_chat(chat)
        dialog = scenario.get("dialog")
        if dialog == "preferences":
            GLib.timeout_add(600, lambda: app.show_preferences() and False)
        elif dialog == "devices":
            GLib.timeout_add(600, lambda: w.conversation.show_devices() and False)
        elif dialog == "details":
            GLib.timeout_add(600, lambda: w.conversation.show_details() and False)
        GLib.timeout_add(2500, shoot)
        return False

    app.connect("activate", lambda *_: GLib.timeout_add(800, setup))
    with loop.running():
        app.run([sys.argv[0]])
    return status["code"]


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        return child(sys.argv[2], sys.argv[3])
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    names = sys.argv[2:] or list(SCENARIOS)
    failed = 0
    for n in names:
        rc = run_scenario(n, os.path.join(out_dir, n + ".png"))
        if rc:
            print("FAILED:", n, file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
