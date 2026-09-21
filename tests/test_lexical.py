"""`memo.lexical` — BM25 relevance over symbol name + one-line doc.

Requires `rank_bm25` to exercise the real scoring path; the extras-absent fallback
is tested separately by forcing `_try_bm25()` to return None, so these tests don't
silently pass-by-skipping in an environment that happens to have it installed.
"""

from __future__ import annotations

import pytest

from memo import lexical
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions

CS = """\
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

bm25 = pytest.importorskip("rank_bm25", reason="optional [search] extra not installed")


def build(repo, files):
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for rel, body in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return c


def test_available_true_when_installed():
    assert lexical.available() is True


def test_tokenize_splits_camel_case():
    assert lexical._tokenize("IsRetroEligible") == ["is", "retro", "eligible"]


def test_tokenize_lowercases_and_strips_punctuation():
    assert lexical._tokenize("retry_payment! (please)") == ["retry", "payment", "please"]


def test_build_corpus_only_includes_logic_kinds(repo):
    c = build(repo, {"src/PaymentService.cs": CS})
    items, docs = lexical.build_corpus(c.list_entries())
    names = [s["name"] for _e, s in items]
    assert "HandleTxnError" in names
    assert "LogInfo" in names
    assert "PaymentService" not in names  # a class, not a method/function
    assert len(items) == len(docs)


def test_score_surfaces_doc_match_over_name_mismatch(repo):
    """The whole point: a query sharing vocabulary with a symbol's DOC, not its
    NAME, should still score that symbol meaningfully — this is what exact/substring
    name matching elsewhere in memo structurally cannot do."""
    c = build(repo, {"src/PaymentService.cs": CS})
    scores = lexical.score("retry failed payment", c.list_entries())
    entry = next(e for e in c.list_entries() if e.path.endswith("PaymentService.cs"))

    handle_line = next(s["line"] for s in entry.structured["symbols"]
                        if s["name"] == "HandleTxnError")
    log_line = next(s["line"] for s in entry.structured["symbols"]
                     if s["name"] == "LogInfo")

    assert scores[(entry.path, handle_line)] > scores[(entry.path, log_line)]


def test_score_empty_query_returns_empty(repo):
    c = build(repo, {"src/PaymentService.cs": CS})
    assert lexical.score("", c.list_entries()) == {}
    assert lexical.score("   ", c.list_entries()) == {}


def test_score_no_entries_returns_empty():
    assert lexical.score("retry payment", []) == {}


def test_available_false_when_forced_absent(monkeypatch):
    monkeypatch.setattr(lexical, "_try_bm25", lambda: None)
    assert lexical.available() is False


def test_score_returns_empty_when_forced_absent(monkeypatch, repo):
    monkeypatch.setattr(lexical, "_try_bm25", lambda: None)
    c = build(repo, {"src/PaymentService.cs": CS})
    assert lexical.score("retry failed payment", c.list_entries()) == {}
