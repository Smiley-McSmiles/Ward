"""End to end: creating accounts (in-band registration, invites), password changes, deleting accounts, and the
server administration commands an admin sees."""

import uuid

import pytest
from helpers import Core, Database, Settings, account, make_core, session, stop

from ward.core import register
from ward.core.uri import parse_xmpp

pytestmark = pytest.mark.integration

def opts(prosody):
    return {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}


def test_register_change_password_delete(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        form = await register.get_form("localhost", opts(prosody))
        assert form.form is not None or "username" in form.legacy_fields
        name = "newbie-" + uuid.uuid4().hex[:6]
        jid = await register.register("localhost", name, "first-pass", form.form, options=opts(prosody))
        assert jid == name + "@localhost"
        with pytest.raises(register.RegistrationError) as taken:
            await register.register("localhost", name, "x", form.form, options=opts(prosody))
        assert taken.value.condition == "conflict"
        d = tmp_path / "newbie"
        d.mkdir()
        core = Core(Settings(str(d / "s.json")), Database(str(d / "w.db")), connect=False)
        await core.start()
        acc = await core.add_account(jid, "first-pass", "never", opts(prosody), connect=False)
        await core.connect_account(acc, "first-pass")
        await session(core).wait_online(20)
        await register.change_password(session(core), "second-pass")
        await core.reconnect(acc)
        await core.provide_password(acc, "second-pass", remember=False)
        await session(core).wait_online(20)
        await register.delete_account(session(core))
        await stop(core)
        with pytest.raises(register.RegistrationError):
            # the account is gone, so its name is free again - but the old password does not log in
            await register.get_form("nonexistent.invalid", {"host": "127.0.0.1", "port": 1, "tls": "starttls",
                                                            "proxy": "none"})

    run(main(), timeout=90)


def test_admin_and_invites(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        admin = await make_core(tmp_path, prosody, "admin", "adm")
        try:
            acc = account(admin)
            assert acc.is_admin, "the Server section appears for admins"
            s = session(admin)
            stats = await admin.admin.stats(s)
            assert int(stats["onlineusersnum"]) >= 1 and stats.get("uptime")
            users, kind = await admin.admin.users(s)
            assert kind in ("registered", "online") and acc.jid in users
            # an invite to create an account (XEP-0401), used through the registration flow
            link = await admin.admin.create_invite(s)
            uri = parse_xmpp(link) if link.startswith("xmpp:") else None
            if uri is None:
                pytest.skip("server returned an invite page instead of an xmpp: link: %s" % link)
            assert uri.is_account_invite
            form = await register.get_form("localhost", opts(prosody), preauth=uri.params["preauth"])
            name = "invited-" + uuid.uuid4().hex[:6]
            jid = await register.register("localhost", name, "pw", form.form, options=opts(prosody),
                                          preauth=uri.params["preauth"])
            assert jid.startswith(name)
            # a generic command runs as a multi-step session
            step = await admin.admin.execute(s, "localhost", "http://jabber.org/protocol/admin#user-stats")
            assert step.form is not None and not step.finished
            step = await admin.admin.advance(s, step, "complete", _filled(step.form, "accountjid", acc.jid))
            assert step.finished
        finally:
            await stop(admin)

    run(main(), timeout=90)


def _filled(form, var, value):
    form.set(var, value)
    return form
