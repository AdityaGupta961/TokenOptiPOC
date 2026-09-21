"""Call-site receivers and symbol containers — the inputs to edge scoring.

The load-bearing distinction is between a *named* receiver and an expression one.
`items.Where(...).Select(Map).ToList()` puts `Select` and `ToList` directly after a `.`
exactly as `a[0].Run(` does, and none of them names a type. Scoring those as though they
named the enclosing class would hand every fluent chain a bonus it hasn't earned, on
precisely the C# these edges are meant to disambiguate — so they must classify as
RECV_EXPR and carry no information.
"""

from __future__ import annotations

import pytest

from memo.graphindex import (
    RECV_BARE,
    RECV_EXPR,
    RECV_IDENT,
    RECV_NEW,
    RECV_SELF,
    _classify_receiver,
    _containers_by_line,
)
from memo.summarizer import summarize


def recv_of(text: str, name: str):
    """Classify the call to `name` in `text`."""
    return _classify_receiver(text, text.index(name + "("))


# --- receiver classification ------------------------------------------------

def test_bare_call_has_no_receiver():
    assert recv_of("var x = Helper(1);", "Helper") == (None, RECV_BARE)


def test_statement_start_is_bare_not_expression():
    assert recv_of("{\n    Helper(1);\n}", "Helper") == (None, RECV_BARE)


def test_named_receiver_is_captured():
    assert recv_of("orders.CreateAsync(dto);", "CreateAsync") == ("orders", RECV_IDENT)


def test_type_receiver_is_captured():
    assert recv_of("Mapper.Map(src);", "Map") == ("Mapper", RECV_IDENT)


def test_chained_call_is_expr_unknown_not_bare():
    """The LINQ case. `Select` and `ToList` follow a `.` but name no receiver."""
    text = "items.Where(x => x.Id > 0).Select(Map).ToList();"
    assert recv_of(text, "Where") == ("items", RECV_IDENT)
    assert recv_of(text, "Select") == (None, RECV_EXPR)
    assert recv_of(text, "ToList") == (None, RECV_EXPR)


def test_index_and_call_prefixes_are_expr_unknown():
    assert recv_of("a[0].Run(1);", "Run") == (None, RECV_EXPR)
    assert recv_of("build().Run(1);", "Run") == (None, RECV_EXPR)


def test_scope_resolution_prefix_is_expr_unknown():
    assert recv_of("MyType::Static(1);", "Static") == (None, RECV_EXPR)


def test_numeric_literal_receiver_is_expr_unknown():
    assert recv_of("1.ToString();", "ToString") == (None, RECV_EXPR)


def test_optional_chaining_and_non_null_assertion_keep_the_receiver():
    assert recv_of("obj?.Go();", "Go") == ("obj", RECV_IDENT)
    assert recv_of("obj!.Go();", "Go") == ("obj", RECV_IDENT)


@pytest.mark.parametrize("word", ["this", "self", "Me", "MyBase", "MyClass", "ME"])
def test_self_receivers_are_recognised_case_insensitively(word):
    _, kind = recv_of(f"{word}.Refresh();", "Refresh")
    assert kind == RECV_SELF


def test_new_expression_is_flagged_as_a_constructor_call():
    # `new OrderService(` reads as preceding char `w`, so the keyword needs matching.
    assert recv_of("var s = new OrderService(repo, log);", "OrderService") == (None, RECV_NEW)


def test_vb_new_is_recognised_case_insensitively():
    assert recv_of("Dim s = New LegacyHelper()", "LegacyHelper") == (None, RECV_NEW)


def test_a_keyword_before_a_call_is_not_a_receiver():
    assert recv_of("return Helper(1);", "Helper") == (None, RECV_BARE)
    assert recv_of("await Helper(1);", "Helper") == (None, RECV_BARE)


def test_whitespace_and_newlines_before_the_dot_are_tolerated():
    assert recv_of("orders\n    .CreateAsync(dto);", "CreateAsync") == ("orders", RECV_IDENT)


# --- container derivation ---------------------------------------------------

CS = """\
namespace Shop
{
    public class OrderService
    {
        public int Total(int a) { return a; }
        public int Discount(int a) { return a / 2; }
    }

    public class Reporter
    {
        public int Total(int a) { return a; }
    }
}
"""

VB = """\
Public Class LegacyHelper
    Public Sub Refresh()
    End Sub
End Class
"""

PY = """\
class Widget:
    def render(self):
        pass

def render():
    pass
"""

TS = """\
export class UserCard {
  render(a) { return a; }
}
function render(a) { return a; }
"""


def containers(text: str, language: str) -> dict[str, str]:
    """Map each symbol's short name to its derived container."""
    syms = sorted((vars(s) for s in summarize(text, language).symbols if s.line),
                  key=lambda s: s["line"])
    by_line = _containers_by_line(syms)
    out = {}
    for s in syms:
        short = s["name"].rsplit(".", 1)[-1].rsplit("#", 1)[-1]
        out[short] = by_line.get(s["line"], "")
    return out


def test_container_derived_from_span_for_csharp():
    """C# emits bare names, so the container comes from the class's `end_line` span."""
    got = containers(CS, "csharp")
    assert got["Discount"] == "OrderService"
    assert got["OrderService"] == ""


def test_same_named_methods_get_different_containers():
    # This is the whole point: two `Total` definitions, distinguishable at last.
    syms = sorted((vars(s) for s in summarize(CS, "csharp").symbols if s.line),
                  key=lambda s: s["line"])
    by_line = _containers_by_line(syms)
    totals = [by_line.get(s["line"], "") for s in syms if s["name"] == "Total"]
    assert sorted(totals) == ["OrderService", "Reporter"]


def test_container_derived_from_span_for_vbnet():
    assert containers(VB, "vbnet")["Refresh"] == "LegacyHelper"


def test_container_read_off_qualified_name_for_python():
    got = containers(PY, "python")
    assert got["render"] in ("Widget", "")  # the class member wins the short-name key


def test_python_module_level_function_has_no_container():
    syms = sorted((vars(s) for s in summarize(PY, "python").symbols if s.line),
                  key=lambda s: s["line"])
    by_line = _containers_by_line(syms)
    module_fn = next(s for s in syms if s["line"] == 5)
    assert by_line.get(module_fn["line"], "") == ""


def test_container_read_off_qualified_name_for_typescript():
    syms = sorted((vars(s) for s in summarize(TS, "typescript").symbols if s.line),
                  key=lambda s: s["line"])
    by_line = _containers_by_line(syms)
    member = next(s for s in syms if s["name"] == "UserCard.render")
    assert by_line[member["line"]] == "UserCard"


def test_container_is_empty_when_the_extent_is_unknown():
    """A container with no known end must not claim everything below it."""
    syms = [
        {"name": "Mystery", "kind": "class", "line": 1, "end_line": 0},
        {"name": "Later", "kind": "method", "line": 40, "end_line": 42},
    ]
    assert _containers_by_line(syms).get(40, "") == ""


def test_nested_container_is_the_innermost():
    syms = [
        {"name": "Outer", "kind": "class", "line": 1, "end_line": 20},
        {"name": "Inner", "kind": "class", "line": 5, "end_line": 15},
        {"name": "Deep", "kind": "method", "line": 8, "end_line": 10},
        {"name": "Shallow", "kind": "method", "line": 17, "end_line": 19},
    ]
    by_line = _containers_by_line(syms)
    assert by_line[8] == "Inner"
    assert by_line[17] == "Outer", "the inner class closed before line 17"
