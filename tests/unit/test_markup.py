from hypothesis import given, settings
from hypothesis import strategies as st

from ward.core.markup import emoji, parse, parse_markdown, parse_styling, preview, to_html, to_plain
from ward.core.markup.tree import (
    BOLD,
    CODE,
    DIRECTIVE,
    ITALIC,
    LINK,
    MENTION,
    SPOILER,
    STRIKE,
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    Quote,
    Rule,
    Table,
    links,
)


def runs(doc, i=0):
    return [(r.text, set(r.styles)) for r in doc.blocks[i].inlines]


# ------------------------------------------------------------------------------------------------ Markdown
def test_inline_styles():
    d = parse_markdown("**b** *i* ~~s~~ `c`")
    assert runs(d) == [("b", {BOLD}), (" ", set()), ("i", {ITALIC}), (" ", set()), ("s", {STRIKE}),
                       (" ", set()), ("c", {CODE})]


def test_newlines_are_line_breaks():
    d = parse_markdown("one\ntwo")
    assert runs(d) == [("one\ntwo", set())]


def test_raw_html_is_text_and_images_are_links():
    d = parse_markdown("<script>alert(1)</script> ![cat](https://example.org/cat.png)")
    texts = [r.text for r in d.blocks[0].inlines]
    assert "<script>alert(1)</script> " in texts
    img = d.blocks[0].inlines[-1]
    assert img.text == "cat" and img.href == "https://example.org/cat.png" and LINK in img.styles


def test_unsafe_links_are_not_clickable():
    d = parse_markdown("[click](javascript:alert(1)) [file](file:///etc/passwd)")
    assert all(not r.href for r in d.blocks[0].inlines)


def test_autolinks_trim_punctuation():
    d = parse_markdown("see https://example.org/a_(b) and https://x.org/y. or www.ward.im!")
    hrefs = [r.href for r in d.blocks[0].inlines if r.href]
    assert hrefs == ["https://example.org/a_(b)", "https://x.org/y", "https://www.ward.im"]


def test_spoilers_pair_up():
    d = parse_markdown("a ||one|| b ||two|| c ||")
    sp = [(r.text, r.spoiler_id) for r in d.blocks[0].inlines if SPOILER in r.styles]
    assert sp == [("one", 1), ("two", 2)]
    assert d.blocks[0].inlines[-1].text.endswith("||")
    assert "▒▒▒" in preview(d) and "one" not in preview(d)


def test_spoilers_skip_code():
    d = parse_markdown("`||x||` ||y||")
    assert [r.text for r in d.blocks[0].inlines if SPOILER in r.styles] == ["y"]


def test_mentions_only_known_nicks():
    d = parse_markdown("hi @Alex and @bob, mail me@example.org", nicknames={"alex"})
    ms = [r.text for r in d.blocks[0].inlines if MENTION in r.styles]
    assert ms == ["@Alex"]


def test_blocks():
    md = ("# Title\n\n> quote\n>> nested\n\n- [x] done\n- [ ] todo\n- plain\n\n3. three\n4. four\n\n"
          "| a | b |\n|:-:|--:|\n| 1 | 2 |\n\n```py\nprint(1)\n```\n\n---\n")
    d = parse_markdown(md)
    kinds = [type(b) for b in d.blocks]
    assert kinds == [Heading, Quote, ListBlock, ListBlock, Table, CodeBlock, Rule]
    assert isinstance(d.blocks[1].children[1], Quote)
    tasks = d.blocks[2]
    assert [it.checked for it in tasks.items] == [True, False, None]
    assert d.blocks[3].ordered and d.blocks[3].start == 3
    assert d.blocks[4].align == ["center", "right"]
    assert d.blocks[5].language == "py" and d.blocks[5].text == "print(1)"


def test_indented_code_is_not_a_code_block():
    d = parse_markdown("    indented line")
    assert isinstance(d.blocks[0], Paragraph)


def test_links_collected():
    d = parse_markdown("[a](https://a.example) https://b.example\n> https://a.example")
    assert links(d) == ["https://a.example", "https://b.example"]


def test_plain_and_html_export():
    d = parse_markdown("**hi** <b>\n\n- one\n- two")
    assert to_plain(d) == "hi <b>\n• one\n• two"
    h = to_html(d)
    assert "<strong>hi</strong> &lt;b&gt;" in h and "<ul>" in h


# ---------------------------------------------------------------------------------- XEP-0393 Message Styling
def test_styling_spec_examples():
    d = parse_styling("I am *strong* and _emphasised_ and ~struck~ and `pre *formatted*`")
    styled = [(t, s) for t, s in runs(d) if s and DIRECTIVE not in s]
    assert styled == [("strong", {BOLD}), ("emphasised", {ITALIC}), ("struck", {STRIKE}),
                      ("pre *formatted*", {CODE})]


def test_styling_rules():
    # not opened after a word character, not followed by whitespace, not closed after whitespace
    for text in ("not*strong*", "* not strong*", "*not strong *", "**"):
        d = parse_styling(text)
        assert all(BOLD not in s for _t, s in runs(d)), text
    # nesting
    d = parse_styling("*_both_*")
    assert ("both", {BOLD, ITALIC}) in runs(d)
    # spans do not cross lines
    d = parse_styling("*one\ntwo*")
    assert all(BOLD not in s for _t, s in runs(d))


def test_styling_blocks():
    d = parse_styling("> quoted\n>> deeper\nplain\n```\n*not styled*\n```\nafter")
    assert isinstance(d.blocks[0], Quote)
    assert isinstance(d.blocks[0].children[1], Quote)
    assert isinstance(d.blocks[2], CodeBlock) and d.blocks[2].text == "*not styled*"
    unterminated = parse_styling("```\ncode until the end")
    assert unterminated.blocks[0].text == "code until the end"


def test_parse_picks_the_right_rules():
    assert BOLD in parse("*x*", is_markdown=False).blocks[0].inlines[1].styles
    assert ITALIC in parse("*x*", is_markdown=True).blocks[0].inlines[0].styles
    assert ITALIC in parse("*x*", is_markdown=False, markdown_from_others=True).blocks[0].inlines[0].styles


# ------------------------------------------------------------------------------------------------ emoji
def test_emoji_helpers():
    assert emoji.replace_shortcodes("party :tada: :nope:") == "party 🎉 :nope:"
    assert emoji.replace_smileys("hi :) see http://x:)") == "hi 🙂 see http://x:)"
    assert emoji.complete(":thu")[0] == ("thumbsdown", "👎") or emoji.complete(":thu")[0][0].startswith("thu")
    assert emoji.emoji_only("👍") == 1
    assert emoji.emoji_only("👨‍👩‍👧 🇫🇷 1️⃣") == 3
    assert emoji.emoji_only("👍👍👍👍") == 0
    assert emoji.emoji_only("ok 👍") == 0


# ------------------------------------------------------------------------------------------------ fuzzing
@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=300))
def test_parsers_never_crash(text):
    for doc in (parse_markdown(text, {"alex"}), parse_styling(text, {"alex"})):
        to_plain(doc)
        to_html(doc)
        preview(doc)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="*_~`|> \n[]()!#-1.:@abcxhttps/", max_size=120))
def test_parsers_never_crash_on_syntax_soup(text):
    parse_markdown(text, {"a"})
    parse_styling(text)
