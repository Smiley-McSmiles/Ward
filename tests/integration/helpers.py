"""Two (or more) real Ward cores talking to the test Prosody server."""

from __future__ import annotations

import asyncio
import os
import time

from ward.core.core import Core
from ward.core.storage.db import Database
from ward.settings import Settings


async def wait_for(cond, timeout: float = 15, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for %s" % what)
        await asyncio.sleep(0.05)


async def make_core(tmp_path, prosody, user: str, name: str | None = None, direct: bool = False,
                    resource: str | None = None, **options) -> Core:
    d = tmp_path / (name or user)
    d.mkdir(exist_ok=True)
    core = Core(Settings(str(d / "settings.json")), Database(str(d / "ward.db")), connect=False)
    await core.start()
    opts = {"host": "127.0.0.1", "port": prosody.c2s_tls if direct else prosody.c2s,
            "tls": "direct" if direct else "starttls", "proxy": "none"}
    if resource:
        opts["resource"] = resource
    opts.update(options)
    acc = await core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
    await core.connect_account(acc, "password")
    s = core.sessions[acc.id]
    await s.wait_online(20)
    # wait for the login steps (disco, roster, archive catch-up...) to finish
    await wait_for(lambda: acc.step == "", 20, "login steps of %s" % user)
    return core


def account(core: Core):
    return core.accounts.list()[0]


def session(core: Core):
    return core.sessions[account(core).id]


async def befriend(a: Core, b: Core) -> None:
    """a adds b, b accepts: mutual presence subscription."""
    acc_a, acc_b = account(a), account(b)
    await a.roster.add_contact(session(a), acc_b.jid, name="B")
    await wait_for(lambda: b.requests.find(acc_b.id, acc_a.jid) or (
        b.roster.contacts.get(acc_b.id, acc_a.jid) is not None and b.roster.contacts.get(acc_b.id, acc_a.jid).sees_us),
        what="request at b")
    if b.requests.find(acc_b.id, acc_a.jid):
        await b.accept_request(acc_b, acc_a.jid)
    await wait_for(lambda: (a.roster.contacts.get(acc_a.id, acc_b.jid) or _N).subscription == "both", 15,
                   "a's subscription to b")
    await wait_for(lambda: (b.roster.contacts.get(acc_b.id, acc_a.jid) or _N).subscription == "both", 15,
                   "b's subscription to a")


class _N:
    subscription = ""
    show = "offline"


async def stop(*cores: Core) -> None:
    for c in cores:
        await c.shutdown()


os.environ.setdefault("WARD_LOG", "warning")
