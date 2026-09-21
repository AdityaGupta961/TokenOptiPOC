"""`memo adr` — Architecture Decision Records linked to symbols."""

from __future__ import annotations

from memo import adr


def test_add_assigns_incrementing_ids(repo):
    r1 = adr.add(repo, "Use flat JSON, not a DB")
    r2 = adr.add(repo, "Static analysis only, no LLM")
    assert r1["id"] == 1
    assert r2["id"] == 2


def test_add_persists_across_loads(repo):
    adr.add(repo, "Decision A")
    assert len(adr.list_all(repo)) == 1
    # A fresh read from disk, not a cached object.
    assert adr.list_all(repo)[0]["title"] == "Decision A"


def test_get_by_id(repo):
    rec = adr.add(repo, "Decision A")
    assert adr.get(repo, rec["id"])["title"] == "Decision A"
    assert adr.get(repo, 9999) is None


def test_update_status(repo):
    rec = adr.add(repo, "Decision A")
    assert adr.update_status(repo, rec["id"], "superseded")
    assert adr.get(repo, rec["id"])["status"] == "superseded"


def test_update_status_unknown_id_returns_false(repo):
    assert not adr.update_status(repo, 42, "superseded")


def test_for_symbol_matches_affects_list(repo):
    adr.add(repo, "Cache design", affects=["Cache", "CacheEntry"])
    adr.add(repo, "Unrelated", affects=["Renderer"])
    hits = adr.for_symbol(repo, "Cache")
    assert len(hits) == 1
    assert hits[0]["title"] == "Cache design"


def test_for_symbol_is_token_aware_not_substring(repo):
    """"Cache" must not match an unrelated ADR just because the word appears
    somewhere in a longer identifier that isn't actually about it."""
    adr.add(repo, "Constructor injection", affects=["Constructor"])
    assert adr.for_symbol(repo, "Cache") == []


def test_for_symbol_no_match_is_empty(repo):
    adr.add(repo, "Decision A", affects=["Foo"])
    assert adr.for_symbol(repo, "Bar") == []


def test_render_functions_do_not_crash(repo):
    rec = adr.add(repo, "Decision A", context="ctx", decision="dec",
                   consequences="cons", affects=["Foo"])
    records = adr.list_all(repo)
    assert adr.render_list(records, terse=True)
    assert adr.render_list(records, terse=False)
    assert adr.render_one(rec, terse=True)
    assert adr.render_one(rec, terse=False)
    assert adr.render_for("Foo", adr.for_symbol(repo, "Foo"))


def test_render_list_empty_is_a_real_answer(repo):
    assert "No ADRs" in adr.render_list([])
