"""Reference counting in `map`.

`_count_refs` counts distinct identifiers times their frequency instead of testing
every occurrence — source is ~22x repetitive, and the naive form was 1,763 ms of a
2,300 ms `memo map` (file reads were only 149 ms of it). These tests exist to prove
the faster form is *exactly* equivalent, since a ranking that silently drifts is far
worse than a slow one.
"""

from __future__ import annotations

import pytest

from memo.mapper import _IDENT, _collapse, _count_refs, _matches

TEXT = """\
public class SiteService {
    private UctSiteRequest request;
    public UctSiteResponse MapToUctSiteRequest(UctSiteRequest request) {
        return Map(request, request, request);
    }
    // site site site
}
"""


def naive(text: str, qc: str) -> int:
    """The original implementation, kept as the oracle."""
    return sum(1 for m in _IDENT.finditer(text) if _matches(m.group(0), qc))


@pytest.mark.parametrize("query", [
    "site", "Site", "uct", "Uct", "request", "Map", "service",
    "response", "zzz", "s", "SiteService", "MapToUctSiteRequest",
])
def test_matches_the_naive_implementation(query):
    qc = _collapse(query)
    assert _count_refs(TEXT, qc) == naive(TEXT, qc)


def test_counts_every_occurrence_not_just_distinct_names():
    """The frequency multiply is the whole point — three `request` args are three refs."""
    qc = _collapse("request")
    assert _count_refs(TEXT, qc) == naive(TEXT, qc) > 3


def test_shared_cache_does_not_leak_between_query_terms():
    """Regression: keying the memo cache on identifier alone returns the FIRST term's
    verdict for every later term, silently corrupting multi-term ranking."""
    cache: dict = {}
    site = _collapse("site")
    uct = _collapse("uct")

    # Warm the cache on one term, then ask a different one over the same identifiers.
    first = _count_refs(TEXT, site, cache)
    second = _count_refs(TEXT, uct, cache)

    assert first == naive(TEXT, site)
    assert second == naive(TEXT, uct)
    assert first != second, "fixture must use terms with different counts"


def test_cache_reuse_across_calls_is_stable():
    qc = _collapse("site")
    cache: dict = {}
    counts = [_count_refs(TEXT, qc, cache) for _ in range(3)]
    assert len(set(counts)) == 1
    assert counts[0] == naive(TEXT, qc)


def test_cache_is_optional():
    qc = _collapse("uct")
    assert _count_refs(TEXT, qc) == _count_refs(TEXT, qc, {})


def test_empty_text_and_no_match():
    assert _count_refs("", _collapse("site")) == 0
    assert _count_refs(TEXT, _collapse("nonexistentterm")) == 0


def test_token_boundary_matching_is_preserved():
    """The camelCase/acronym awareness is what separates this from grep noise."""
    text = "Uct UctConfig MapToUctSiteRequest UCTId Constructor Product Instruction"
    qc = _collapse("uct")
    assert _count_refs(text, qc) == naive(text, qc)
    # Constructor/Product/Instruction contain "uct" as a substring but not as a token.
    assert _count_refs(text, qc) == 4
