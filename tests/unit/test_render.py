"""Inline-code hit ranges for click-to-copy in rendered Markdown."""

from ward.core.markup.tree import CODE, Run
from ward.gui.render import _wheel_needs_forwarding, _wheel_step, inline_code_ranges


def test_inline_code_ranges_use_character_offsets():
    runs = [Run("see "), Run("x", frozenset({CODE})), Run(" and "),
            Run("y", frozenset({CODE})), Run(".")]
    assert inline_code_ranges(runs) == [(4, 5, "x"), (10, 11, "y")]


def test_inline_code_ranges_handle_unicode_and_empty_runs():
    runs = [Run("hé "), Run(""), Run("💡", frozenset({CODE}))]
    assert inline_code_ranges(runs) == [(3, 4, "💡")]


def test_wheel_step_matches_gtk_and_survives_a_zero_page():
    # GtkScrolledWindow scrolls one detent by page_size ** (2/3); forwarding must not outrun it.
    assert abs(_wheel_step(900) - 900 ** (2 / 3)) < 1e-9
    assert _wheel_step(0) == 48.0


def test_wheel_forwarding_is_off_on_newer_gtk():
    from ward.gui.compat import GTK
    # Newer GTK hands the unconsumed wheel to the parent itself; doing both doubled the jump.
    assert _wheel_needs_forwarding() is (GTK < (4, 16))
