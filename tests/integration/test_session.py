import asyncio

import pytest

from ward.core.models import AccountItem
from ward.core.session import AccountSession

pytestmark = pytest.mark.integration


def make(prosody, user="alice", password="password", direct=False, **opts):
    acc = AccountItem(id=1, jid="%s@localhost" % user, enabled=True)
    acc.options.update({"host": "127.0.0.1", "port": prosody.c2s_tls if direct else prosody.c2s,
                        "tls": "direct" if direct else "starttls", "proxy": "none", **opts})
    return AccountSession(acc), password


@pytest.mark.parametrize("direct", [False, True])
def test_connect_and_disco(trust_test_ca, run, direct):
    sess, pw = make(trust_test_ca, direct=direct)

    async def main():
        await sess.start(pw)
        await sess.wait_online(20)
        from xml.etree import ElementTree as ET
        feats = set()
        for target in ("localhost", "alice@localhost"):
            res = await sess.iq("get", target, ET.Element("{http://jabber.org/protocol/disco#info}query"))
            feats |= {f.get("var") for f in res.iter("{http://jabber.org/protocol/disco#info}feature")}
        bound = sess.bound_jid
        await sess.stop()
        return feats, bound

    feats, bound = run(main())
    assert "urn:xmpp:mam:2" in feats and "urn:xmpp:carbons:2" in feats
    assert bound.startswith("alice@localhost/ward.")
    assert sess.account.state == "offline"


def test_wrong_password(trust_test_ca, run):
    sess, _ = make(trust_test_ca, password="nope")

    async def main():
        await sess.start("nope")
        for _ in range(100):
            if sess.account.state == "error":
                break
            await asyncio.sleep(0.1)
        kind, wanted = sess.account.error_kind, sess.wanted
        await sess.stop()
        return kind, wanted

    kind, wanted = run(main())
    assert kind == "auth"
    assert not wanted


@pytest.mark.parametrize("direct", [False, True])
def test_untrusted_certificate_can_be_pinned(prosody, run, monkeypatch, direct):
    monkeypatch.delenv("WARD_EXTRA_CA", raising=False)
    sess, pw = make(prosody, direct=direct)
    problems = []
    sess.connect("cert-problem", lambda _s, info: problems.append(info))

    async def main():
        await sess.start(pw)
        for _ in range(100):
            if problems:
                break
            await asyncio.sleep(0.1)
        assert problems, sess.account.error
        info = problems[0]
        sess.trust_certificate(info)
        await sess.wait_online(20)
        await sess.stop()
        # next time the pin is used straight away
        sess2, _ = make(prosody, direct=direct)
        sess2.account.options["pinned_certs"] = sess.account.options["pinned_certs"]
        await sess2.start(pw)
        await sess2.wait_online(20)
        await sess2.stop()
        return info

    info = run(main(), timeout=60)
    assert sess.account.error_kind in ("", "certificate")
    assert "localhost" in info.names and len(info.sha256) == 64
