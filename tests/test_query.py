"""`memo query` — the ad-hoc predicate filter over symbols/edges."""

from __future__ import annotations

import pytest

from memo import graphindex as gi
from memo import query
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions

PROD = """\
using System;
namespace Shop {
  public class OrderService {
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
    public int Lonely(int a) { return a; }
  }
}
"""


def build(repo, files):
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for rel, body in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return gi.build(c.list_entries())


def graph(repo):
    return build(repo, {"src/OrderService.cs": PROD})


# --- parsing ------------------------------------------------------------- #

def test_defaults_to_symbols_target():
    target, preds = query.parse("kind=method")
    assert target == "symbols"
    assert preds == [("kind", "=", "method")]


def test_explicit_edges_target():
    target, preds = query.parse("edges callee=Add")
    assert target == "edges"
    assert preds == [("callee", "=", "Add")]


def test_rejects_unknown_field():
    with pytest.raises(query.QueryError):
        query.parse("symbols bogus=1")


def test_rejects_missing_operator():
    with pytest.raises(query.QueryError):
        query.parse("symbols kindmethod")


def test_quoted_values_with_spaces():
    target, preds = query.parse('symbols name~"Order Service"')
    assert preds == [("name", "~", "Order Service")]


# --- evaluation ------------------------------------------------------------ #

def test_symbols_filter_by_kind_and_name(repo):
    g = graph(repo)
    r = query.run(g, "symbols kind=method name=Add")
    names = {row["name"] for row in r["rows"]}
    assert names == {"Add"}


def test_symbols_callers_field_reflects_call_graph(repo):
    g = graph(repo)
    r = query.run(g, "symbols name=Add")
    assert r["rows"][0]["callers"] == 1  # Total() calls Add()


def test_symbols_contains_operator(repo):
    g = graph(repo)
    r = query.run(g, "symbols path~OrderService")
    assert r["total"] >= 3  # Total, Add, Lonely all live in that file


def test_symbols_zero_callers_finds_leaf_methods(repo):
    g = graph(repo)
    r = query.run(g, "symbols kind=method callers=0")
    names = {row["name"] for row in r["rows"]}
    assert "Lonely" in names
    assert "Add" not in names


def test_edges_filter_by_callee(repo):
    g = graph(repo)
    r = query.run(g, "edges callee=Add")
    assert r["total"] == 1
    assert r["rows"][0]["caller"] == "Total"


def test_limit_truncates_but_reports_total(repo):
    g = graph(repo)
    r = query.run(g, "symbols kind=method", limit=1)
    assert r["shown"] == 1
    assert r["total"] == 3


def test_render_terse_and_full_do_not_crash(repo):
    g = graph(repo)
    r = query.run(g, "symbols kind=method")
    assert query.render(r, terse=True)
    assert query.render(r, terse=False)


def test_render_empty_result_says_no_matches(repo):
    g = graph(repo)
    r = query.run(g, "symbols name=NoSuchMethod")
    assert "no matches" in query.render(r, terse=True).lower() \
        or "No matches" in query.render(r, terse=False)
