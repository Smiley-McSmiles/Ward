"""Inline-code hit ranges for click-to-copy in rendered Markdown."""

from ward.core.markup.tree import CODE, Run
from ward.gui.render import inline_code_ranges


def test_inline_code_ranges_use_character_offsets():
    runs = [Run("see "), Run("x", frozenset({CODE})), Run(" and "),
            Run("y", frozenset({CODE})), Run(".")]
    assert inline_code_ranges(runs) == [(4, 5, "x"), (10, 11, "y")]


def test_inline_code_ranges_handle_unicode_and_empty_runs():
    runs = [Run("hé "), Run(""), Run("💡", frozenset({CODE}))]
    assert inline_code_ranges(runs) == [(3, 4, "💡")]
