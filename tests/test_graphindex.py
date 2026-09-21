"""Symbol extents and call-site attribution.

The `end_line` work exists because the old "a symbol runs until the next symbol"
heuristic collapses containers: a class is immediately followed by its own first
method, so the class appeared to be one line long. That made `peek <Class>` return a
truncated body and mis-attributed calls to the wrong enclosing method.
"""

from __future__ import annotations

import pytest

from memo.analyzers.base import (
    block_extent_if_braced, brace_extent, line_at, line_of, line_starts, vb_block_end,
)
from memo.graphindex import _body_range, _enclosing
from memo.summarizer import summarize

CS = """\
using System;

namespace Shop
{
    public class OrderService
    {
        private readonly int _rate;

        public int Total(int a)
        {
            return a * _rate;
        }

        public int Discount(int a)
        {
            return a / 2;
        }
    }
}
"""


def syms(text, language):
    """Structured symbol dicts, sorted by line — the shape graphindex consumes."""
    out = [vars(s) for s in summarize(text, language).symbols if s.line]
    return sorted(out, key=lambda s: s["line"])


# --- extents ----------------------------------------------------------------

def test_class_span_includes_its_methods():
    s = syms(CS, "csharp")
    cls = next(x for x in s if x["kind"] == "class")
    idx = s.index(cls)
    start, end = _body_range(s, idx, len(CS.splitlines()))
    assert start == 5
    assert end == 18, "the class must span past its own methods, not stop at the first"


def test_last_method_does_not_overrun_into_closing_braces():
    s = syms(CS, "csharp")
    last = next(x for x in s if x["name"] == "Discount")
    start, end = _body_range(s, s.index(last), len(CS.splitlines()))
    assert (start, end) == (14, 17)


def test_body_range_falls_back_when_extent_unknown():
    """Symbols without end_line keep the old next-symbol behaviour."""
    fake = [{"line": 10, "end_line": 0}, {"line": 20, "end_line": 0}]
    assert _body_range(fake, 0, 100) == (10, 19)
    assert _body_range(fake, 1, 100) == (20, 100)


def test_body_range_never_returns_inverted_span():
    fake = [{"line": 30, "end_line": 5}]  # nonsense extent
    start, end = _body_range(fake, 0, 100)
    assert end >= start


def test_body_range_clamps_to_file_length():
    fake = [{"line": 5, "end_line": 9999}]
    assert _body_range(fake, 0, 40) == (5, 40)


def test_python_extents_are_exact():
    src = "class A:\n    def m(self):\n        return 1\n\n    def n(self):\n        return 2\n"
    s = syms(src, "python")
    cls = next(x for x in s if x["kind"] == "class")
    assert (cls["line"], cls["end_line"]) == (1, 6)
    m = next(x for x in s if x["name"] == "A.m")
    assert (m["line"], m["end_line"]) == (2, 3)


# --- enclosing-symbol attribution -------------------------------------------

def test_enclosing_prefers_the_innermost_symbol():
    s = syms(CS, "csharp")
    assert _enclosing(s, 11)["name"] == "Total"
    assert _enclosing(s, 16)["name"] == "Discount"


def test_enclosing_falls_back_to_class_between_members():
    """Line 7 is a field: inside the class but inside no method."""
    s = syms(CS, "csharp")
    assert _enclosing(s, 7)["kind"] == "class"


def test_enclosing_skips_symbols_that_already_closed():
    """Line 18 is the class's closing brace — past Discount's end, so not Discount."""
    s = syms(CS, "csharp")
    assert _enclosing(s, 18)["name"] != "Discount"


def test_enclosing_returns_none_above_the_first_symbol():
    s = syms(CS, "csharp")
    assert _enclosing(s, 1) is None


# --- brace scanning edge cases ----------------------------------------------

def test_brace_extent_ignores_braces_in_strings_and_comments():
    text = 'void F()\n{\n    var s = "{ not a brace }";\n    // }\n    /* } */\n}\n'
    assert brace_extent(text, text.index("{")) == 6


def test_brace_extent_handles_verbatim_strings():
    text = 'void F()\n{\n    var s = @"a \\ { b";\n}\n'
    assert brace_extent(text, text.index("{")) == 4


def test_brace_extent_returns_zero_when_unbalanced():
    assert brace_extent("void F()\n{\n  oops\n", 0) == 0
    assert brace_extent("no braces here", 0) == 0


def test_block_extent_distinguishes_expression_bodies():
    braced = "const f = () => {\n  return 1;\n}\n"
    assert block_extent_if_braced(braced, braced.index("=>") + 2) == 3
    expr = "const C = () => <div/>;\n"
    assert block_extent_if_braced(expr, expr.index("=>") + 2) == 0


# --- offset->line lookup ----------------------------------------------------

@pytest.mark.parametrize("text", [
    "",
    "one line no newline",
    "a\nb\nc\n",
    "\n\n\n",
    "trailing\n\n",
    "no final newline\nsecond",
    "windows\r\nstyle\r\n",
])
def test_line_at_matches_line_of_everywhere(text):
    """`line_at` must be a drop-in for `line_of`, which it replaced on hot paths:
    `line_of` re-counts newlines from offset 0 on every call, making repeated lookups
    quadratic in file size (821 ms vs 8 ms for 3,019 lookups in one 842 KB file)."""
    starts = line_starts(text)
    for i in range(len(text) + 1):
        assert line_at(starts, i) == line_of(text, i), f"offset {i} in {text!r}"


def test_line_starts_counts_lines():
    assert line_starts("a\nb\nc") == [0, 2, 4]
    assert line_starts("") == [0]


def test_line_at_is_one_based():
    starts = line_starts("first\nsecond\n")
    assert line_at(starts, 0) == 1
    assert line_at(starts, 6) == 2


def test_vb_block_end_matches_nested_blocks():
    lines = [
        "Public Class Helper",       # 1
        "    Public Sub Go()",       # 2
        "        Return",            # 3
        "    End Sub",               # 4
        "End Class",                 # 5
    ]
    assert vb_block_end(lines, 2, "Sub") == 4
    assert vb_block_end(lines, 1, "Class") == 5
