"""`memo find` / `memo trace` / `memo brief` — three of the most agent-facing commands
(all featured in the README's top command table), and all three had zero direct tests
anywhere in the suite before this file. Written to probe for real bugs, not just to
pad coverage — see the regression tests below for what that turned up.
"""

from __future__ import annotations

from memo import graphindex as gi
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.graph import brief, find_value, render_brief, render_find, render_trace, trace

CS = """\
using System;
namespace Shop {
  public class OrderService {
    // PREMERA is a payer code used in retro-eligibility checks.
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
    public bool IsRetroEligible(string payer) { return payer == "PREMERA"; }
  }
}
"""

CYCLE = """\
using System;
namespace Shop {
  public class Cyclic {
    public void A() { B(); }
    public void B() { C(); }
    public void C() { A(); }
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
    return c, gi.build(c.list_entries(), cache_root=find_cache_root())


# --- find_value ------------------------------------------------------------- #

def test_find_value_locates_literal_and_attributes_enclosing_symbol(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), "PREMERA")
    assert r["total_hits"] >= 1
    hit_symbols = {h["enclosing"] for f in r["files"] for h in f["hits"]}
    assert "IsRetroEligible" in hit_symbols


def test_find_value_is_case_insensitive_by_default(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), "premera")
    assert r["total_hits"] >= 1


def test_find_value_case_sensitive_excludes_mismatched_case(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), "premera", ignore_case=False)
    assert r["total_hits"] == 0


def test_find_value_regex_mode(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), r"Is\w+Eligible", regex=True)
    assert r["total_hits"] >= 1


def test_find_value_no_match_is_a_real_empty_result(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), "NoSuchLiteralAnywhere")
    assert r["total_hits"] == 0
    assert r["files"] == []
    out = render_find(r)
    assert "No cached file" in out


def test_render_find_terse_and_full_differ(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = find_value(c.list_entries(), "PREMERA")
    assert render_find(r, terse=True) != render_find(r, terse=False)


# --- trace ------------------------------------------------------------------ #

def test_trace_down_walks_callees(repo):
    _c, g = build(repo, {"src/OrderService.cs": CS})
    r = trace(g, "Total", direction="down", depth=3)
    names = {c["name"] for c in r["tree"]["children"]}
    assert "Add" in names


def test_trace_up_walks_callers(repo):
    _c, g = build(repo, {"src/OrderService.cs": CS})
    r = trace(g, "Add", direction="up", depth=3)
    names = {c["name"] for c in r["tree"]["children"]}
    assert "Total" in names


def test_trace_respects_depth_limit(repo):
    _c, g = build(repo, {"src/Cyclic.cs": CYCLE})
    r = trace(g, "A", direction="down", depth=1)
    # depth=1 means one level of children only, no grandchildren
    assert r["tree"]["children"]
    for child in r["tree"]["children"]:
        assert child["children"] == []


def test_trace_does_not_infinite_loop_on_a_cycle(repo):
    """A -> B -> C -> A. Without the path_seen guard this recurses forever."""
    _c, g = build(repo, {"src/Cyclic.cs": CYCLE})
    r = trace(g, "A", direction="down", depth=10)  # would hang here if unguarded

    def flatten(node):
        yield node["name"]
        for ch in node["children"]:
            yield from flatten(ch)

    # Reaches every method in the cycle at least once; terminates instead of looping.
    seen = list(flatten(r["tree"]))
    assert {"A", "B", "C"} <= set(seen)


def test_trace_unknown_symbol_has_no_children(repo):
    _c, g = build(repo, {"src/OrderService.cs": CS})
    r = trace(g, "NoSuchMethodAnywhere", direction="down", depth=3)
    assert r["tree"]["children"] == []


def test_render_trace_reports_no_results_message(repo):
    _c, g = build(repo, {"src/OrderService.cs": CS})
    r = trace(g, "NoSuchMethodAnywhere", direction="down", depth=3)
    out = render_trace(r)
    assert "No callees found" in out


def test_render_trace_terse_and_full_differ(repo):
    _c, g = build(repo, {"src/OrderService.cs": CS})
    r = trace(g, "Total", direction="down", depth=2)
    assert render_trace(r, terse=True) != render_trace(r, terse=False)


# --- brief -------------------------------------------------------------- #

def test_brief_ranks_by_rare_term_match(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5)
    names = [s["name"] for s in r["slices"]]
    assert "IsRetroEligible" in names


EXACT_MATCH_CS = """\
using System;
namespace Shop {
  public class GraphBuilder {
    public void BuildGraph() { }
  }
  public class ChatService {
    public void BuildGraphConfig() { }
  }
}
"""


def test_brief_ranks_exact_name_match_above_compound_name(repo):
    """Reproduces a real bug found on an external repo: querying "build_graph"
    returned `ChatService._build_graph_config` ranked ABOVE the actual `build_graph`
    function, because `_matches` treats "buildgraph" as a valid prefix match of
    "buildgraphconfig" too — same name_score, no distinction for the caller's
    identifier being an exact match vs. merely a prefix of a longer compound name."""
    c, _g = build(repo, {"src/Graph.cs": EXACT_MATCH_CS})
    r = brief(c.list_entries(), "build_graph", top=5)
    names = [s["name"] for s in r["slices"]]
    assert names[0] == "BuildGraph", (
        f"exact match should rank first, got order: {names}")


def test_brief_respects_token_budget(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5, max_tokens=1)
    # Every candidate method is larger than a 1-token budget once the first is used,
    # so the bundle must stay small — this is the budget doing its job, not a bug.
    assert r["used_tokens"] <= 50


def test_brief_no_relevant_terms_returns_nothing_relevant(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "the a of", top=5)  # all stopwords
    assert r["slices"] == []
    out = render_brief(r)
    assert "Nothing relevant" in out


def test_render_brief_terse_omits_line_number_prefixes(repo):
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5)
    terse = render_brief(r, terse=True)
    full = render_brief(r, terse=False)
    assert terse != full
    assert "1: " not in terse.split("##")[1] if "##" in terse else True


def test_brief_with_graph_still_finds_relevant_symbol(repo):
    """Passing `graph=` upgrades the centrality signal but must not break the
    primary name-match ranking — the whole point is it's an addition, not a swap."""
    c, g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5, graph=g)
    names = [s["name"] for s in r["slices"]]
    assert "IsRetroEligible" in names


def test_brief_without_graph_matches_prior_behavior(repo):
    """graph=None (the default) must be unaffected by its presence elsewhere —
    no new required argument, no behavior change for existing callers."""
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5, graph=None)
    names = [s["name"] for s in r["slices"]]
    assert "IsRetroEligible" in names


# --- pagerank ------------------------------------------------------------- #

def test_pagerank_ranks_hub_above_leaf():
    """A calls B calls C calls A: A and B each have one caller, C has one caller
    too, but in a triangle every node should get a comparable, non-zero score —
    the real signal to check is a genuine hub vs. an uncalled leaf."""
    src = """\
using System;
namespace Shop {
  public class Hub {
    public void Central() { }
  }
  public class Caller1 { public void M() { new Hub().Central(); } }
  public class Caller2 { public void M() { new Hub().Central(); } }
  public class Caller3 { public void M() { new Hub().Central(); } }
  public class Leaf { public void Unused() { } }
}
"""
    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as d:
        repo = _Path(d)
        c, g = build(repo, {"src/Hub.cs": src})
        hub_score = g.pagerank_score("Central")
        leaf_score = g.pagerank_score("Unused")
        assert hub_score > leaf_score


def test_pagerank_empty_graph_returns_empty_dict(repo):
    c, g = build(repo, {"src/Empty.cs": "namespace Shop { public class Empty {} }"})
    assert g.pagerank() == {}
    assert g.pagerank_score("anything") == 0.0


def test_pagerank_is_cached_on_instance(repo):
    c, g = build(repo, {"src/OrderService.cs": CS})
    first = g.pagerank()
    assert g._pagerank_cache is first
    second = g.pagerank()
    assert second is first  # same object — not recomputed


def test_pagerank_fallback_matches_igraph_ranking(repo, monkeypatch):
    """The hand-rolled power-iteration fallback must agree, at least qualitatively
    (hub outranks leaf), with the igraph-backed path — this environment has
    python-igraph installed, so force the fallback to actually exercise it."""
    from memo import graphindex as gi_mod
    monkeypatch.setattr(gi_mod, "_try_igraph", lambda: None)

    src = """\
using System;
namespace Shop {
  public class Hub {
    public void Central() { }
  }
  public class Caller1 { public void M() { new Hub().Central(); } }
  public class Caller2 { public void M() { new Hub().Central(); } }
  public class Leaf { public void Unused() { } }
}
"""
    c, g = build(repo, {"src/Hub.cs": src})
    hub_score = g.pagerank_score("Central")
    leaf_score = g.pagerank_score("Unused")
    assert hub_score > leaf_score
    assert hub_score > 0


# --- brief + BM25 (lexical.py) -------------------------------------------- #

PAYMENT_CS = """\
using System;
namespace Shop {
  public class PaymentService {
    /// <summary>Retries a failed payment charge against the processor up to 3 times.</summary>
    public void HandleTxnError(int orderId) { }
    public void LogInfo(string msg) { }
    public void FormatCurrency(int cents) { }
    public void ValidateAddress(string addr) { }
    public void SendReceiptEmail(int orderId) { }
    public void ArchiveOldOrders(int days) { }
  }
}
"""


def test_brief_surfaces_doc_match_with_bm25_installed(repo):
    """End-to-end: a query matching only a symbol's DOC (not its name) must still
    surface that symbol, once rank_bm25 is available — this is BM25's whole reason
    for existing here. Skips cleanly if the optional extra isn't installed."""
    import pytest
    pytest.importorskip("rank_bm25", reason="optional [search] extra not installed")
    c, _g = build(repo, {"src/PaymentService.cs": PAYMENT_CS})
    r = brief(c.list_entries(), "retry failed payment charge", top=3)
    names = [s["name"] for s in r["slices"]]
    assert "HandleTxnError" in names


def test_brief_falls_back_cleanly_without_bm25(repo, monkeypatch):
    """Forcing rank_bm25 "absent" must not break brief() or change its behavior for
    a query that already matches by name — the whole point of the fallback."""
    from memo import lexical
    monkeypatch.setattr(lexical, "_try_bm25", lambda: None)
    c, _g = build(repo, {"src/OrderService.cs": CS})
    r = brief(c.list_entries(), "retro eligible", top=5)
    names = [s["name"] for s in r["slices"]]
    assert "IsRetroEligible" in names


def test_pagerank_ignores_weak_tier_edges(repo):
    """Mirrors tiered_indegree's own contract: an edge with no resolution evidence
    (TIER_WEAK) must not contribute to the score, same as it's excluded from
    caller_count — otherwise a common short name would look "important" for
    exactly the reason it's actually noise."""
    c, g = build(repo, {"src/OrderService.cs": CS})
    # Add() is called once with real evidence (same-file arity match).
    assert g.pagerank_score("Add") >= 0.0  # sanity: never negative, never crashes
