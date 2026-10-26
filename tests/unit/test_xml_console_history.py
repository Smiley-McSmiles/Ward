"""The XML console keeps recent stanzas in the session, bounded, even while its window is closed."""

from __future__ import annotations

from ward.core.models import AccountItem
from ward.core.session import XML_HISTORY_BYTES, AccountSession


def make_session(enabled: bool = True) -> AccountSession:
    return AccountSession(AccountItem(id=1, jid="alice@example.org"), {"advanced.devtools": enabled})


def test_xml_history_is_kept_while_the_console_is_closed() -> None:
    session = make_session()
    assert not session.console

    session._console_in("<message xmlns='jabber:client'><body>hello</body></message>")
    session._console_out("<presence xmlns='jabber:client'/>")

    assert len(session.xml_history) == 2
    first, second = session.xml_history
    assert first[1] is True and "hello" in first[2]
    assert second[1] is False and "<presence" in second[2]

    session.console = True
    session._console_in("<message xmlns='jabber:client'><body>live</body></message>")
    assert len(session.xml_history) == 3 and "live" in session.xml_history[-1][2]


def test_xml_history_is_masked_and_bounded() -> None:
    session = make_session()
    session._console_in("<iq xmlns='jabber:client'><password>hunter2</password></iq>")
    assert "hunter2" not in session.xml_history[-1][2]

    stanza = "<message xmlns='jabber:client'><body>%s</body></message>" % ("x" * 200_000)
    for _ in range(8):
        session._console_in(stanza)

    assert session.xml_history_size <= XML_HISTORY_BYTES
    assert sum(len(text.encode("utf-8")) for _stamp, _incoming, text in session.xml_history) <= XML_HISTORY_BYTES
    assert len(session.xml_history) < 8

    session.clear_xml_history()
    assert not session.xml_history and session.xml_history_size == 0


def test_xml_history_is_off_without_developer_tools() -> None:
    session = make_session(enabled=False)
    session._console_in("<message xmlns='jabber:client'><body>private</body></message>")
    assert not session.xml_history

    # turning developer tools off drops anything kept before
    session.settings["advanced.devtools"] = True
    session._console_in("<message xmlns='jabber:client'><body>kept</body></message>")
    assert session.xml_history
    session.settings["advanced.devtools"] = False
    session._console_in("<message xmlns='jabber:client'><body>private again</body></message>")
    assert not session.xml_history
