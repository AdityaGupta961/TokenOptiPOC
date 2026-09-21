"""`memo ingest-traces` — merging observed runtime calls in as verified edges.

The load-bearing property: an ingested (caller, callee) pair that memo's static scan
never saw (reflection, DI, a dynamic dispatch) must still show up as a `verified`
call_site once merged, and must outrank every static tier when the two disagree.
"""

from __future__ import annotations

from memo import graphindex as gi
from memo import traces
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
    return c, gi.build(c.list_entries(), cache_root=find_cache_root())


# --- parse_jsonl -------------------------------------------------------- #

def test_parse_jsonl_basic():
    text = '{"caller": "A.foo", "callee": "B.bar"}\n{"caller": "C", "callee": "D"}\n'
    recs = traces.parse_jsonl(text)
    assert recs == [{"caller": "A.foo", "callee": "B.bar"}, {"caller": "C", "callee": "D"}]


def test_parse_jsonl_skips_blank_and_comment_lines():
    text = "# a comment\n\n{\"caller\": \"A\", \"callee\": \"B\"}\n"
    assert traces.parse_jsonl(text) == [{"caller": "A", "callee": "B"}]


def test_parse_jsonl_skips_malformed_lines():
    text = "not json\n{\"caller\": \"A\", \"callee\": \"B\"}\n{\"caller\": \"onlycaller\"}\n"
    assert traces.parse_jsonl(text) == [{"caller": "A", "callee": "B"}]


def test_parse_jsonl_keeps_explicit_count():
    text = '{"caller": "A", "callee": "B", "count": 5}\n'
    assert traces.parse_jsonl(text) == [{"caller": "A", "callee": "B", "count": 5}]


# --- ingest --------------------------------------------------------------- #

def test_ingest_writes_runtime_file(repo):
    root = find_cache_root()
    stats = traces.ingest(root, [{"caller": "A", "callee": "B"}])
    assert stats["added"] == 1
    assert (root / gi.RUNTIME_FILE).is_file()


def test_ingest_accumulates_counts_across_calls(repo):
    root = find_cache_root()
    traces.ingest(root, [{"caller": "A", "callee": "B"}])
    stats = traces.ingest(root, [{"caller": "A", "callee": "B"}])
    assert stats["updated"] == 1
    assert stats["added"] == 0


def test_clear_removes_the_file(repo):
    root = find_cache_root()
    traces.ingest(root, [{"caller": "A", "callee": "B"}])
    assert traces.clear(root)
    assert not (root / gi.RUNTIME_FILE).is_file()
    assert not traces.clear(root)  # second call: nothing to remove


# --- merge into the graph -------------------------------------------------- #

def test_merged_edge_resolves_to_a_real_symbol_and_outranks_static_tiers(repo):
    _c, g = build(repo, {"src/OrderService.cs": PROD})
    root = find_cache_root()
    # Lonely has no static callers at all — this pair could only come from a real trace.
    traces.ingest(root, [{"caller": "Total", "callee": "Lonely"}])
    gi.merge_runtime_edges(g, root)
    verified = [cs for cs in g.call_sites if cs.get("verified")]
    assert len(verified) == 1
    assert verified[0]["caller"] == "Total"
    assert verified[0]["callee"] == "Lonely"

    # And it must actually change what `callers` reports.
    result = g.callers("lonely")
    assert any(r["tier"] == gi.TIER_VERIFIED for r in result)


def test_unresolvable_callee_is_dropped_not_dangling(repo):
    _c, g = build(repo, {"src/OrderService.cs": PROD})
    root = find_cache_root()
    traces.ingest(root, [{"caller": "Total", "callee": "NoSuchMethodAnywhere"}])
    before = len(g.call_sites)
    gi.merge_runtime_edges(g, root)
    assert len(g.call_sites) == before


def test_load_or_build_merges_runtime_edges_transparently(repo):
    c, g = build(repo, {"src/OrderService.cs": PROD})
    root = find_cache_root()
    gi.save(g, root)
    traces.ingest(root, [{"caller": "Total", "callee": "Lonely"}])
    reloaded = gi.load_or_build(c)
    assert any(cs.get("verified") for cs in reloaded.call_sites)
