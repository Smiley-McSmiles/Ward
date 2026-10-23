"""Attribution and the donation options shown in the About dialog."""

from ward import __version__, credits


def test_donation_options_are_complete():
    assert [k for k, _ in credits.DONATION_OPTIONS] == ["BTC", "XMR", "CashApp"]
    values = dict(credits.DONATION_OPTIONS)
    assert values["BTC"].startswith("bc1")
    assert len(values["XMR"]) == 95                 # a Monero main address is 95 characters
    assert values["CashApp"].startswith("$")


def test_copyright_is_safe_as_pango_markup():
    # The About dialog parses the copyright as markup: a bare "&" would make GTK drop the whole string.
    assert "&amp;" in credits.COPYRIGHT_MARKUP
    assert "&amp;" not in credits.COPYRIGHT


def test_about_text_lists_the_donations():
    text = credits.about_text()
    assert "Ward %s" % __version__ in text
    assert "Donate" in text
    for _name, value in credits.DONATION_OPTIONS:
        assert value in text
