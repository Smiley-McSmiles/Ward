import datetime as dt
from xml.etree import ElementTree as ET

from _fuzz import given, settings, st

from ward.core import colors, forms, search, stanzas, uri
from ward.core.chat import ChatService
from ward.core.stanzas import q


# ------------------------------------------------------------------------------------------------- URIs
def test_xmpp_uris():
    u = uri.parse_xmpp("xmpp:romeo@montague.net?message;body=Hello%20there")
    assert (u.jid, u.action, u.params) == ("romeo@montague.net", "message", {"body": "Hello there"})
    u = uri.parse_xmpp("xmpp:room@conference.example.org?join;password=s3cret")
    assert u.action == "join" and u.params["password"] == "s3cret"
    fp = "a" * 64
    u = uri.parse_xmpp("xmpp:alice@example.org?omemo-sid-123=%s;omemo-sid-456=%s;omemo-sid-7=bad" % (fp, fp.upper()))
    assert u.omemo == {123: fp, 456: fp}
    u = uri.parse_xmpp("xmpp:inviter@example.org?roster;preauth=TOKEN;ibr=y")
    assert u.is_invite and u.is_account_invite
    u = uri.parse_xmpp("xmpp:example.org?register;preauth=T")
    assert u.is_account_invite
    assert uri.parse_xmpp("https://example.org") is None
    assert uri.parse_xmpp("xmpp:") is None


def test_xmpp_uri_roundtrip():
    s = uri.build_xmpp("alice@example.org", "message", {"body": "hi & bye; ok?"}, {5: "ab" * 32})
    u = uri.parse_xmpp(s)
    assert u.params["body"] == "hi & bye; ok?" and u.omemo == {5: "ab" * 32}


def test_aesgcm_links():
    iv, key = bytes(range(12)), bytes(range(32))
    link = uri.build_aesgcm("https://upload.example.org/f/cat.jpg", iv, key)
    p = uri.parse_aesgcm(link)
    assert (p.https_url, p.iv, p.key) == ("https://upload.example.org/f/cat.jpg", iv, key)
    p16 = uri.parse_aesgcm("aesgcm://h/f#" + (bytes(16) + key).hex())
    assert len(p16.iv) == 16
    assert uri.parse_aesgcm("aesgcm://h/f#abc") is None
    assert uri.parse_aesgcm("https://h/f#" + (iv + key).hex()) is None


def test_aesgcm_links_in_message_bodies():
    frag = (bytes(range(12)) + bytes(range(32))).hex()
    # Ward for Android: "<name>\n<url>", with the uploaded path ending in .enc
    body, files = ChatService._body_aesgcm(
        None, "holiday photo.jpg\naesgcm://upload.example.org/f/holiday%20photo.jpg.enc#" + frag)
    assert body == ""
    assert len(files) == 1
    assert files[0]["name"] == "holiday photo.jpg"
    assert files[0]["url"].startswith("aesgcm://") and files[0]["aes_key"] and files[0]["aes_iv"]
    # a caption stays on the message; the name comes from the upload path
    body, files = ChatService._body_aesgcm(None, "Look at this\naesgcm://upload.example.org/f/cat.jpg#" + frag)
    assert body == "Look at this"
    assert len(files) == 1 and files[0]["name"] == "cat.jpg"
    # only the link, as Ward itself sends
    body, files = ChatService._body_aesgcm(None, "aesgcm://upload.example.org/f/cat.jpg#" + frag)
    assert body == "" and len(files) == 1 and files[0]["name"] == "cat.jpg"


def test_aesgcm_body_file_gets_a_mime_type():
    from types import SimpleNamespace

    from ward.core.files import FileService

    class _Files:
        guess_mime = staticmethod(FileService.guess_mime)

    svc = SimpleNamespace(core=SimpleNamespace(files=_Files()))
    svc._body_aesgcm = lambda body: ChatService._body_aesgcm(svc, body)
    frag = (bytes(range(12)) + bytes(range(32))).hex()
    found, body = ChatService._attachments(
        svc, stanzas.ParsedMessage(), "cat.jpg\naesgcm://upload.example.org/f/cat.jpg.enc#" + frag)
    assert body == "" and len(found) == 1
    assert found[0]["name"] == "cat.jpg" and found[0]["mime"] == "image/jpeg"


def test_geo():
    g = uri.parse_geo("geo:48.2010,16.3695,183;u=10")
    assert (g.lat, g.lon, g.alt, g.uncertainty) == (48.201, 16.3695, 183.0, 10.0)
    assert uri.parse_geo("geo:91,0") is None


# ------------------------------------------------------------------------------------------------ colours
def test_xep0392_test_vectors():
    # From XEP-0392 version 0.8, section "Test vectors" (HSLuv, S=100, L=50)
    vectors = {"Romeo": (327.255249, (0.865, 0.000, 0.686)),
               "juliet@capulet.lit": (209.410400, (0.000, 0.515, 0.573)),
               "😺": (331.199341, (0.872, 0.000, 0.659)),
               "council": (359.994507, (0.918, 0.000, 0.394))}
    for text, (angle, rgb) in vectors.items():
        assert abs(colors.hue_angle(text) - angle) < 1e-5
        assert all(abs(a - b) < 0.002 for a, b in zip(colors.rgb(text), rgb, strict=True))
    assert colors.hex_color("Romeo").startswith("#")
    assert colors.initials("Alex Smith") == "AS" and colors.initials("bob@example.org") == "BE"


# ------------------------------------------------------------------------------------------------ forms
FORM = """<x xmlns='jabber:x:data' type='form'>
  <title>Register</title><instructions>Fill it in</instructions>
  <field var='FORM_TYPE' type='hidden'><value>jabber:iq:register</value></field>
  <field var='username' type='text-single' label='User'><required/></field>
  <field var='password' type='text-private' label='Password'><required/></field>
  <field var='color' type='list-single' label='Colour'><value>red</value>
    <option label='Red'><value>red</value></option><option label='Blue'><value>blue</value></option></field>
  <field var='ocr' type='text-single' label='Enter the text'>
    <media xmlns='urn:xmpp:media-element' height='80' width='290'>
      <uri type='image/png'>cid:sha1+abc@bob.xmpp.org</uri></media></field>
  <field type='fixed'><value>Just text</value></field>
</x>"""


def test_forms_parse_and_submit():
    stanza = ET.fromstring("<iq xmlns='jabber:client'><query xmlns='jabber:iq:register'>%s</query>"
                           "<data xmlns='urn:xmpp:bob' cid='sha1+abc@bob.xmpp.org' type='image/png'>iVBORw==</data>"
                           "</iq>" % FORM)
    f = forms.find(stanza.find(q("jabber:iq:register", "query")), stanza)
    assert f.title == "Register" and f.form_type == "jabber:iq:register"
    assert [x.var for x in f.missing_required()] == ["username", "password"]
    assert f.get("color").options == [("red", "Red"), ("blue", "Blue")]
    assert f.get("ocr").media[0].data == b"\x89PNG"
    f.set("username", "juliet")
    f.set("password", "pw")
    assert f.missing_required() == []
    sub = forms.to_element(f)
    assert sub.get("type") == "submit"
    back = forms.parse(sub)
    assert back.value("username") == "juliet" and back.value("FORM_TYPE") == "jabber:iq:register"
    assert all(x.type != "fixed" for x in back.fields)


# ------------------------------------------------------------------------------------------------ stanzas
def msg(inner, attrs="type='chat' from='a@x/r' to='b@y' id='m1'"):
    return ET.fromstring("<message xmlns='jabber:client' %s>%s</message>" % (attrs, inner))


def test_parse_rich_message():
    m = stanzas.parse(msg(
        "<body>&gt; Alex:\n&gt; hi\nhello</body><origin-id xmlns='urn:xmpp:sid:0' id='o1'/>"
        "<stanza-id xmlns='urn:xmpp:sid:0' by='B@y' id='s1'/><request xmlns='urn:xmpp:receipts'/>"
        "<markable xmlns='urn:xmpp:chat-markers:0'/><active xmlns='http://jabber.org/protocol/chatstates'/>"
        "<reply xmlns='urn:xmpp:reply:0' to='b@y' id='orig'/>"
        "<fallback xmlns='urn:xmpp:fallback:0' for='urn:xmpp:reply:0'><body start='0' end='13'/></fallback>"
        "<markdown xmlns='urn:xmpp:ward:markdown:0'/><store xmlns='urn:xmpp:hints'/>"
        "<delay xmlns='urn:xmpp:delay' stamp='2026-01-02T03:04:05.123456789Z'/>"))
    assert m.origin_id == "o1" and m.stanza_ids == {"b@y": "s1"}
    assert m.receipt_request and m.markable and m.chat_state == "active" and m.markdown
    assert m.reply_to == ("b@y", "orig")
    assert m.display_body() == "hello"
    assert m.hints == {"store"}
    assert abs(m.delay - dt.datetime(2026, 1, 2, 3, 4, 5, 123456, dt.UTC).timestamp()) < 1e-3


def test_parse_reactions_corrections_retractions():
    assert stanzas.parse(msg("<reactions xmlns='urn:xmpp:reactions:0' id='x'><reaction>👍</reaction>"
                             "<reaction> </reaction></reactions>")).reactions == ("x", ["👍"])
    assert stanzas.parse(msg("<replace xmlns='urn:xmpp:message-correct:0' id='x'/>")).correction_of == "x"
    assert stanzas.parse(msg("<retract xmlns='urn:xmpp:message-retract:1' id='x'/>")).retraction_of == "x"
    assert stanzas.parse(msg("<apply-to xmlns='urn:xmpp:fasten:0' id='y'><retract "
                             "xmlns='urn:xmpp:message-retract:0'/></apply-to>")).retraction_of == "y"
    mod = stanzas.parse(msg("<retract xmlns='urn:xmpp:message-retract:1' id='z'><moderated "
                            "xmlns='urn:xmpp:message-moderate:1' by='room@muc/mod'><occupant-id "
                            "xmlns='urn:xmpp:occupant-id:0' id='occ'/></moderated><reason>spam</reason></retract>"))
    assert mod.moderation.target == "z" and mod.moderation.reason == "spam" and mod.retraction_of == ""


def test_unwrap_carbons_and_mam():
    carbon = msg("<sent xmlns='urn:xmpp:carbons:2'><forwarded xmlns='urn:xmpp:forward:0'><message "
                 "xmlns='jabber:client' from='b@y/phone' to='a@x' type='chat' id='c1'><body>from my phone</body>"
                 "</message></forwarded></sent>", "from='b@y' to='b@y/ward'")
    m = stanzas.parse(carbon)
    assert m.wrapper.kind == "carbon-sent" and m.body == "from my phone" and m.from_jid == "b@y/phone"
    mam = msg("<result xmlns='urn:xmpp:mam:2' queryid='q1' id='arch1'><forwarded xmlns='urn:xmpp:forward:0'>"
              "<delay xmlns='urn:xmpp:delay' stamp='2026-03-01T00:00:00Z'/><message xmlns='jabber:client' "
              "from='a@x/r' type='chat'><body>old</body></message></forwarded></result>", "from='b@y'")
    m = stanzas.parse(mam)
    assert (m.wrapper.kind, m.wrapper.queryid, m.wrapper.archive_id, m.body) == ("mam", "q1", "arch1", "old")


def test_parse_files_and_errors():
    m = stanzas.parse(msg(
        "<file-sharing xmlns='urn:xmpp:sfs:0'><file xmlns='urn:xmpp:file:metadata:0'><media-type>image/jpeg"
        "</media-type><name>cat.jpg</name><size>1234</size><width>10</width><height>20</height>"
        "<hash xmlns='urn:xmpp:hashes:2' algo='sha-256'>AAAA</hash>"
        "<thumbnail xmlns='urn:xmpp:thumbs:1' uri='data:image/png;base64,iVBORw==' media-type='image/png'/>"
        "</file><sources><url-data xmlns='http://jabber.org/protocol/url-data' target='https://h/cat.jpg'/>"
        "</sources></file-sharing><x xmlns='jabber:x:oob'><url>https://h/cat.jpg</url></x>"))
    f = m.files[0]
    assert (f.name, f.mime, f.size, f.width, f.height, f.url) == ("cat.jpg", "image/jpeg", 1234, 10, 20,
                                                                  "https://h/cat.jpg")
    assert f.sha256 == "000000" and f.thumbnail == b"\x89PNG"
    assert m.oob == [("https://h/cat.jpg", "")]
    e = stanzas.parse(msg("<error type='cancel'><service-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/>"
                          "<text xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'>nope</text></error>",
                          "type='error' id='m9'"))
    assert e.error == ("service-unavailable", "nope")


def test_builders_roundtrip():
    el = ET.Element(q(stanzas.NS_CLIENT, "message"), {"type": "chat", "to": "b@y", "id": "m1"})
    fb = stanzas.reply_fallback("first line\nsecond\n> old quote", "Alex")
    stanzas.set_body(el, fb + "my answer")
    stanzas.add_reply(el, "b@y", "orig", fb)
    stanzas.add_origin_id(el, "m1")
    stanzas.add_markdown_flag(el)
    stanzas.add_reactions(el, "orig", ["🎉"])
    stanzas.add_file_sharing(el, stanzas.FileInfo(url="https://h/f", name="f.txt", mime="text/plain", size=3,
                                                  sha256="ab" * 32, thumbnail=b"x"))
    m = stanzas.parse(el)
    assert m.display_body() == "my answer"
    assert fb.startswith("> Alex:\n> first line\n> second\n")
    assert m.reply_to == ("b@y", "orig") and m.markdown and m.reactions == ("orig", ["🎉"])
    assert m.files[0].sha256 == "ab" * 32 and m.files[0].thumbnail == b"x"
    r = ET.Element(q(stanzas.NS_CLIENT, "message"))
    stanzas.add_retraction(r, "old")
    pr = stanzas.parse(r)
    assert pr.retraction_of == "old" and pr.display_body() == ""
    inside, outside = stanzas.split_content(el)
    assert {e.tag.split("}")[1] for e in inside} >= {"body", "reply", "fallback", "markdown", "reactions"}
    assert {e.tag.split("}")[1] for e in outside} >= {"origin-id", "store"}


@settings(max_examples=150, deadline=None)
@given(st.recursive(st.sampled_from(["<body>x</body>", "<delay xmlns='urn:xmpp:delay' stamp='garbage'/>",
                                     "<reactions xmlns='urn:xmpp:reactions:0'/>",
                                     "<fallback xmlns='urn:xmpp:fallback:0'><body start='9' end='2'/></fallback>",
                                     "<file-sharing xmlns='urn:xmpp:sfs:0'><file xmlns='urn:xmpp:file:metadata:0'>"
                                     "<size>NaN</size><thumbnail xmlns='urn:xmpp:thumbs:1' uri='data:,'/></file>"
                                     "</file-sharing>", "<x xmlns='http://jabber.org/protocol/muc#user'><status "
                                     "code='abc'/><item/></x>", "<forwarded xmlns='urn:xmpp:forward:0'/>"]),
                    lambda children: st.lists(children, max_size=4).map("".join), max_leaves=8))
def test_stanza_parser_never_crashes(inner):
    m = stanzas.parse(msg(inner))
    m.display_body()


# ------------------------------------------------------------------------------------------------ search
def test_search_query_parser():
    qy = search.parse_query('dinner "at noon" from:alex in:family has:file has:bogus before:2026-01-01 after:x')
    assert qy.words == ["dinner"] and qy.phrases == ["at noon"]
    assert qy.from_ == ["alex"] and qy.in_ == ["family"] and qy.has == {"file"}
    assert qy.before == dt.date(2026, 1, 1) and qy.errors == ["has:bogus", "after:x"]
    assert search.fts_expression(qy) == '"dinner"* AND "at noon"'
    nasty = search.parse_query('a" OR b NEAR(c) "')
    assert search.fts_expression(nasty)  # does not raise; everything is quoted
    assert search.parse_query('unterminated "quote').words


def test_search_runs_against_the_database(ward_home, run):
    from ward.core.storage.db import Database

    db = Database()
    db.open()

    def setup(conn):
        conn.execute("INSERT INTO accounts (id, jid, created_at) VALUES (1, 'me@x', 0)")
        conn.execute("INSERT INTO chats (id, account_id, jid, name) VALUES (1, 1, 'alex@x', 'Alex')")
        conn.execute("INSERT INTO chats (id, account_id, jid, name) VALUES (2, 1, 'fam@muc', 'Family')")
        rows = [(1, 1, "alex@x", 0, 1000, "Dinner **tonight**?", 1), (2, 1, "me@x", 1, 2000, "Sure, dinner at 8", 0),
                (3, 2, "mum", 0, 3000, "Family dinner sunday https://x.org", 0)]
        for mid, chat, sender, d, ts, body, md in rows:
            conn.execute("INSERT INTO messages (id, chat_id, sender, direction, timestamp, body, is_markdown) "
                         "VALUES (?,?,?,?,?,?,?)", (mid, chat, sender, d, ts, body, md))
            search.index_message(conn, mid, body, bool(md))

    db.run_sync(setup)

    async def main():
        all_ = await search.search(db, "dinn")
        mine = await search.search(db, "dinner from:me")
        fam = await search.search(db, "dinner in:family has:link")
        tonight = await search.search(db, "tonight")
        return all_, mine, fam, tonight

    all_, mine, fam, tonight = run(main())
    assert [r["id"] for r in all_] == [3, 2, 1]
    assert [r["id"] for r in mine] == [2]
    assert [r["id"] for r in fam] == [3]
    assert "\x02tonight\x03" in tonight[0]["snippet"]      # Markdown stripped before indexing
    db.close()
