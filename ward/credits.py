"""Attribution and donation details shown in the About dialog and the README."""

from __future__ import annotations

from . import ISSUES_URL, URL, __version__
from .i18n import N_

DEVELOPER_NAME = "Ward contributors"
DEVELOPERS = ["Ward contributors"]
COPYRIGHT = "\u00a9 2026 WOOSAH & contributors"
COPYRIGHT_MARKUP = COPYRIGHT.replace("&", "&amp;")   # the About dialog parses it as Pango markup
COMMENTS = N_("A private, modern XMPP client")
WEBSITE = URL
ISSUE_URL = ISSUES_URL
SUPPORT_URL = WEBSITE

# The title of the row in the About dialog; it opens a sub-page (like Credits / Legal) listing the options below.
# Each option is (button title, text copied to the user's clipboard when pressed).
DONATION_BUTTON_LABEL = N_("Donate")
DONATION_PAGE_TITLE = N_("Donate")
DONATION_OPTIONS = [
    ("BTC", "bc1qy2gtdhnfxp9dcs6v9jda748npmsjx3jgwp99mx"),
    ("XMR", "82xtMVSmesuLjPtgHfBCEhM5Fpqh1SLLNf9pzHRRNPqQZsvrnmoM1ZGC7AiLyPfsufdyrMWHrWYV2hsC8jc5rEBVLMHWTLy"),
    ("CashApp", "$SmileyMcSmiles"),
]


def about_text() -> str:
    from .i18n import _

    lines = ["Ward %s" % __version__, _(COMMENTS), "", _("Developers:")]
    lines += ["  - %s" % d for d in DEVELOPERS]
    lines += ["", COPYRIGHT, WEBSITE, "", _("Donate (copy the text):")]
    lines += ["  %-8s %s" % (k, v) for k, v in DONATION_OPTIONS]
    return "\n".join(lines)
