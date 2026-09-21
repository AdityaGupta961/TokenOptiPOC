"""Cache store/index behaviour and console output safety."""

from __future__ import annotations

import io
import os

import pytest

from memo import console
from memo.cache import Cache, sha256_of_file
from memo.config import find_cache_root, language_for, load_extensions


def build(repo, name="a.py", body="def f():\n    return 1\n"):
    """Index one file through the same path the CLI uses."""
    from memo.cli import _build_entry

    p = repo / name
    p.write_text(body, encoding="utf-8")
    exts = load_extensions(find_cache_root())
    entry = _build_entry(p, language_for(p, exts))
    c = Cache(find_cache_root())
    c.store(entry)
    return c, p, entry


def test_store_then_get_round_trips(repo):
    c, p, entry = build(repo)
    got = c.get_by_path(p)
    assert got is not None
    assert got.content_hash == entry.content_hash
    assert got.summary_text == entry.summary_text


def test_is_fresh_tracks_content_changes(repo):
    c, p, _ = build(repo)
    assert c.is_fresh(p)
    p.write_text("def f():\n    return 2\n", encoding="utf-8")
    assert not c.is_fresh(p), "a content change must invalidate the entry"


def test_is_fresh_false_for_deleted_file(repo):
    c, p, _ = build(repo)
    p.unlink()
    assert not c.is_fresh(p)


def test_index_snapshot_matches_list_entries(repo):
    c, _, _ = build(repo, "a.py")
    build(repo, "b.py", "def g():\n    return 2\n")
    snap = c.index_snapshot()
    assert len(snap) == 2
    assert len(c.list_entries()) == 2


def test_identical_content_shares_one_cache_file(repo):
    """Content-hash keying should dedupe two paths with identical bytes."""
    same = "def dup():\n    return 0\n"
    c, _, e1 = build(repo, "one.py", same)
    _, _, e2 = build(repo, "two.py", same)
    assert e1.content_hash == e2.content_hash
    assert len(list((c.cache_dir).glob("*.json"))) == 1
    assert len(c.index_snapshot()) == 2


def test_clear_path_keeps_shared_content_for_other_paths(repo):
    same = "def dup():\n    return 0\n"
    c, p1, _ = build(repo, "one.py", same)
    _, p2, _ = build(repo, "two.py", same)
    assert c.clear_path(p1)
    # p2 still references the same hash, so its entry must survive.
    assert c.get_by_path(p2) is not None


def test_clear_all_empties_the_cache(repo):
    c, _, _ = build(repo)
    c.clear_all()
    assert c.list_entries() == []


@pytest.mark.skipif(os.name != "nt", reason="path case-insensitivity is Windows-specific")
def test_windows_path_casing_does_not_duplicate_entries(repo):
    """On Windows C:\\Repo\\x.py and C:\\repo\\x.py are one file; the cache must agree."""
    c, p, _ = build(repo, "Case.py")
    variant = p.parent / p.name.lower()
    assert c.get_by_path(variant) is not None, \
        "differently-cased path should resolve to the same cache entry"
    assert len(c.index_snapshot()) == 1


# --- console safety: the bug that used to kill piped output ------------------

def test_can_encode_rejects_cp1252_accepts_utf8():
    class S:
        def __init__(self, enc):
            self.encoding = enc

    assert console._can_encode(S("utf-8"))
    assert not console._can_encode(S("cp1252"))
    assert not console._can_encode(S("ascii"))
    assert console._can_encode(S(None)), "unknown encoding must not force ASCII"


def test_ascii_fallback_when_stream_cannot_be_reconfigured(monkeypatch):
    class Unreconfigurable(io.StringIO):
        encoding = "cp1252"

        def reconfigure(self, **kw):
            raise ValueError("cannot reconfigure")

    monkeypatch.setattr("sys.stdout", Unreconfigurable())
    monkeypatch.setattr("sys.stderr", Unreconfigurable())
    console.SYM.update(console.GLYPH)
    try:
        console.init_streams()
        assert console.SYM == console.ASCII
        # And the fallback must actually be encodable where the glyphs were not.
        "".join(console.SYM.values()).encode("cp1252")
    finally:
        console.SYM.update(console.GLYPH)


def test_glyphs_and_ascii_have_matching_keys():
    assert set(console.GLYPH) == set(console.ASCII)
