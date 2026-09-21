"""Lexical (BM25) relevance for `memo brief`'s natural-language queries.

Everywhere else in memo, matching is exact/substring-on-identifier (`mapper.py`'s
`_matches`, `graph.py`'s name-term scoring) — a query only finds a symbol whose *own
name* shares vocabulary with it. "where do we retry failed payments" won't surface
`handle_txn_error` even if that's exactly the right function, because "retry" and
"payments" live in its docstring, not its name. BM25 over each candidate's name plus
one-line doc closes that gap — still lexical (it matches actual words, never
"concepts"), just no longer limited to the symbol's own identifier.

Optional by design, same pattern as tiktoken/python-igraph elsewhere in memo:
`rank_bm25` is a small pure-Python package but still a new dependency, so a missing
install must not break anything. Every entry point here degrades to "no signal"
(`score()` returns `{}`) rather than raising, and `brief()` treats that identically to
"nothing scored" — the existing name-match ranking is untouched when this is absent.
"""

from __future__ import annotations

import re

from .cache import CacheEntry
from .graphindex import _sorted_symbols

_WORD_RE = re.compile(r"[A-Za-z]+")
_LOGIC_KINDS = ("method", "function")


def _try_bm25():
    try:
        from rank_bm25 import BM25Okapi
        return BM25Okapi
    except ImportError:
        return None


def available() -> bool:
    return _try_bm25() is not None


def _tokenize(text: str) -> list[str]:
    """Split identifiers and prose into lowercase words — "IsRetroEligible" ->
    ["is", "retro", "eligible"], the vocabulary a human query would actually use."""
    # Split PascalCase/camelCase boundaries before the word regex, so
    # "RetroEligible" tokenizes as two words instead of one opaque identifier.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [w.lower() for w in _WORD_RE.findall(spaced)]


def build_corpus(entries: list[CacheEntry]) -> tuple[list[tuple[CacheEntry, dict]], list[list[str]]]:
    """One document per candidate (entry, symbol): its name + one-line doc,
    tokenized. Restricted to the same LOGIC-kind symbols `brief()` ranks, so a
    caller can zip its own candidate list against this corpus's scores by
    (path, line) without needing to know this module's internals."""
    items: list[tuple[CacheEntry, dict]] = []
    docs: list[list[str]] = []
    for e in entries:
        for s in _sorted_symbols(e):
            if s.get("kind") not in _LOGIC_KINDS:
                continue
            text = f"{s.get('name', '')} {s.get('doc', '')}"
            items.append((e, s))
            docs.append(_tokenize(text))
    return items, docs


def score(query: str, entries: list[CacheEntry]) -> dict[tuple[str, int], float]:
    """Return {(path, line): bm25_score} for every candidate method/function across
    `entries`. Empty (never raises) if `rank_bm25` isn't installed, there are no
    candidates, or the query tokenizes to nothing (all punctuation/numbers)."""
    bm25_cls = _try_bm25()
    if bm25_cls is None:
        return {}
    items, docs = build_corpus(entries)
    if not docs:
        return {}
    query_tokens = _tokenize(query)
    if not query_tokens:
        return {}
    bm25 = bm25_cls(docs)
    scores = bm25.get_scores(query_tokens)
    return {(e.path, s["line"]): float(sc) for (e, s), sc in zip(items, scores)}
