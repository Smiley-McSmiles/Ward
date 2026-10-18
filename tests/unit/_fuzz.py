"""Hypothesis when it is installed; otherwise the fuzz tests are skipped (some distributions don't package it)."""

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover
    class _Anything:
        def __getattr__(self, _name):
            return self

        def __call__(self, *_a, **_k):
            return self

    st = _Anything()

    def settings(*_a, **_k):
        return lambda f: f

    def given(*_a, **_k):
        return lambda f: pytest.mark.skip(reason="hypothesis is not installed")(f)

__all__ = ["given", "settings", "st"]
