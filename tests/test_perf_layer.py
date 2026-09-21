"""The scale layer: batched index writes, the consolidated manifest, graph v2.

These are pure performance changes, so the tests are mostly about *fidelity* — the
optimisations must not alter what queries see. The measured wins on a 541-file repo
were: list_entries 213ms -> 20ms (one manifest read instead of 540 file reads), and
graph.json 10.7MB -> 3.5MB.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest
from click.testing import CliRunner

from memo import graphindex
from memo.cache import Cache
from memo.cli import _build_entry, main
from memo.config import find_cache_root, language_for, load_extensions


def seed(repo, n=6):
    """Index n small python files, returning the Cache."""
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for i in range(n):
            p = repo / f"m{i}.py"
            p.write_text(f"def f{i}():\n    return helper{i}()\n\ndef helper{i}():\n"
                         f"    return {i}\n", encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return c


# --- batching ---------------------------------------------------------------

def test_batch_writes_index_once_and_keeps_all_entries(repo):
    c = seed(repo, 6)
    assert len(c.index_snapshot()) == 6
    assert len(c.list_entries()) == 6


def test_batch_preserves_entries_from_a_previous_run(repo):
    seed(repo, 3)
    c = Cache(find_cache_root())
    exts = load_extensions(find_cache_root())
    p = repo / "later.py"
    p.write_text("def z():\n    return 0\n", encoding="utf-8")
    with c.batch():
        c.store(_build_entry(p, language_for(p, exts)))
    assert len(c.index_snapshot()) == 4, "a batch must not drop pre-existing index rows"


def test_keep_retains_an_unchanged_entry_during_a_batch(repo):
    """The incremental path calls keep() for skipped files; without it they'd vanish."""
    c = seed(repo, 3)
    index = c.index_snapshot()
    with c.batch():
        for path_key, rec in index.items():
            c.keep(path_key, rec)
    assert c.index_snapshot() == index


# --- manifest ---------------------------------------------------------------

def test_manifest_matches_per_file_reads_exactly(repo):
    c = seed(repo, 6)
    c.write_manifest()
    from_manifest = c.list_entries()
    c._drop_manifest()
    from_files = c.list_entries()

    def norm(entries):
        return sorted((e.path, e.content_hash, e.language, e.summary_text,
                       json.dumps(e.structured, sort_keys=True), e.raw_tokens,
                       e.summary_tokens, e.memo_version) for e in entries)

    assert norm(from_manifest) == norm(from_files)


def test_manifest_is_ignored_when_the_index_moves_on(repo):
    """A stale manifest must never be served — correctness beats the speed win."""
    c = seed(repo, 3)
    c.write_manifest()
    exts = load_extensions(find_cache_root())
    p = repo / "new.py"
    p.write_text("def q():\n    return 1\n", encoding="utf-8")
    c.store(_build_entry(p, language_for(p, exts)))  # outside a batch -> drops manifest
    assert len(c.list_entries()) == 4


def test_manifest_rejected_when_entry_contents_changed_but_index_did_not(repo):
    """Regression: this silently served stale symbols after an analyzer fix.

    Re-analyzing unchanged files rewrites cached symbols while paths and content
    hashes — and therefore index.json — stay byte-identical. Validating the manifest
    against the index alone let it survive, so queries and the rebuilt call graph both
    used the OLD line numbers and the re-index appeared to do nothing.
    """
    c = seed(repo, 4)
    c.write_manifest()
    manifest = json.loads(c.manifest_path.read_text(encoding="utf-8"))
    index_before = dict(c.index_snapshot())

    # Simulate an analyzer change: same files, different cached symbol data.
    manifest["memo_version"] = "0.0.1-ancient"
    for e in manifest["entries"]:
        e["structured"] = {"symbols": [{"name": "STALE", "kind": "method", "line": 1}]}
    c.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert c.index_snapshot() == index_before, "fixture must leave the index untouched"
    names = {s["name"] for e in c.list_entries() for s in e.structured.get("symbols", [])}
    assert "STALE" not in names, "a manifest from another memo version must be ignored"


def test_batch_invalidates_a_preexisting_manifest(repo):
    """A batch can rewrite entry contents without touching the index."""
    c = seed(repo, 3)
    c.write_manifest()
    assert c.manifest_path.is_file()
    with c.batch():
        pass
    assert not c.manifest_path.is_file()


def test_list_entries_self_heals_the_manifest(repo):
    c = seed(repo, 4)
    c._drop_manifest()
    assert not c.manifest_path.exists()
    c.list_entries()
    assert c.manifest_path.is_file(), "first slow call should rebuild the manifest"


def test_corrupt_manifest_falls_back_instead_of_raising(repo):
    c = seed(repo, 3)
    c.write_manifest()
    c.manifest_path.write_text("{not json", encoding="utf-8")
    assert len(c.list_entries()) == 3


def test_duplicate_content_files_each_get_their_own_entry(repo):
    """Regression: two paths sharing one content hash share one cache file, and it
    records only one path. The other silently vanished from list_entries — so it was
    missing from the call graph and re-analyzed on every incremental run."""
    exts = load_extensions(find_cache_root())
    same = "def dup():\n    return 1\n"
    c = Cache(find_cache_root())
    with c.batch():
        for name in ("a.py", "b.py"):
            p = repo / name
            p.write_text(same, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))

    assert len(c.index_snapshot()) == 2
    entries = c.list_entries()
    assert len(entries) == 2, "one entry per indexed path, not per content hash"
    assert {Path(e.path).name for e in entries} == {"a.py", "b.py"}


def test_duplicate_content_files_are_not_reanalyzed_every_run(sample_repo):
    same = "public class Dup { public int X() { return 1; } }\n"
    (sample_repo / "d1.cs").write_text(same, encoding="utf-8")
    (sample_repo / "d2.cs").write_text(same, encoding="utf-8")
    run("init")
    out = run("cache-dir", ".", "-r", "-q").output
    assert "Indexed 0 file(s)" in out, out


def test_graph_shards_produce_an_identical_graph(repo):
    """Shards cache per-file scans; they must not change the resulting graph at all."""
    c = seed(repo, 8)
    entries = c.list_entries()
    full = graphindex.build(entries)                        # no shard cache
    graphindex.build(entries, cache_root=c.root)            # populate shards
    warm = graphindex.build(entries, cache_root=c.root)     # served from shards
    assert (c.root / graphindex.SHARD_FILE).is_file()
    assert norm_graph(warm) == norm_graph(full)


def test_shards_from_another_memo_version_are_ignored(repo):
    c = seed(repo, 4)
    entries = c.list_entries()
    expected = norm_graph(graphindex.build(entries))
    (c.root / graphindex.SHARD_FILE).write_text(json.dumps({
        "memo_version": "0.0.1-ancient",
        "shards": {e.content_hash: {"freq": {}, "cands": [["BOGUS", 1, None, 0]]}
                   for e in entries},
    }), encoding="utf-8")
    assert norm_graph(graphindex.build(entries, cache_root=c.root)) == expected


def test_clear_all_drops_every_derived_artifact(repo):
    """Regression: graph.json/shards.json survived `clear`, so `memo status` reported
    thousands of graph symbols against a zero-file index."""
    c = seed(repo, 4)
    graphindex.save(graphindex.build(c.list_entries(), cache_root=c.root), c.root)
    c.write_manifest()
    assert (c.root / graphindex.GRAPH_FILE).is_file()
    assert (c.root / graphindex.SHARD_FILE).is_file()

    c.clear_all()
    assert not (c.root / graphindex.GRAPH_FILE).exists()
    assert not (c.root / graphindex.SHARD_FILE).exists()
    assert not c.manifest_path.exists()
    assert c.list_entries() == []


def test_clear_all_drops_the_manifest(repo):
    c = seed(repo, 3)
    c.write_manifest()
    c.clear_all()
    assert c.list_entries() == [], "a cleared cache must not keep serving the manifest"


# --- graph serialization ----------------------------------------------------

def norm_graph(g):
    """Every persisted field, so a round-trip that silently drops one is caught.

    Projecting a hand-written subset here is how a new column gets added and never
    verified; the confidence columns were added under exactly this risk.
    """
    return (
        sorted((s["name"], s["qualified"], s["kind"], s["path"], s["line"],
                s.get("end_line", 0), s["is_test"], s["generated"],
                s.get("container", ""), s.get("pmin", -1), s.get("pmax", -1),
                s.get("pflags", 0), s.get("bases", "")) for s in g.symbols),
        sorted(((c["caller"] or ""), c["caller_path"], c["caller_line"], c["callee"],
                c["line"], c["is_test"], c.get("recv") or "",
                c.get("recv_kind", 0), c.get("argc", -1), c.get("recv_type", ""))
               for c in g.call_sites),
        g.freq,
        g.files,
    )


def test_graph_v3_round_trip_is_lossless(repo):
    c = seed(repo, 6)
    g = graphindex.build(c.list_entries())
    graphindex.save(g, c.root)
    assert norm_graph(graphindex.load(c.root)) == norm_graph(g)


def test_graph_saved_as_v4_and_is_smaller_than_v1(repo):
    c = seed(repo, 8)
    g = graphindex.build(c.list_entries())
    graphindex.save(g, c.root)
    data = json.loads((c.root / graphindex.GRAPH_FILE).read_text(encoding="utf-8"))
    assert data["version"] == 4
    assert "paths" in data, "paths must be interned into a table"
    v1_size = len(json.dumps({"version": 1, "symbols": g.symbols,
                              "call_sites": g.call_sites, "freq": g.freq,
                              "files": g.files}))
    assert len(json.dumps(data)) < v1_size


@pytest.mark.parametrize("version,sym_cols,call_cols", [
    (2, 8, 6),    # before receiver/arity/container
    (3, 12, 9),   # before declared types and base lists
])
def test_legacy_graph_loads_with_unknown_confidence(repo, version, sym_cols, call_cols):
    """A .memo written before any resolution column must keep working.

    `load` gates on the integer version only, never on memo_version, so a stale
    graph.json really is served after an upgrade. Missing fields must read as *unknown*
    so scoring finds no evidence, rather than as zeros that look like real measurements.
    """
    c = seed(repo, 5)
    g = graphindex.build(c.list_entries())
    cur = g.to_json()
    (c.root / graphindex.GRAPH_FILE).write_text(json.dumps({
        "version": version,
        "paths": cur["paths"],
        "symbols": [row[:sym_cols] for row in cur["symbols"]],
        "call_sites": [row[:call_cols] for row in cur["call_sites"]],
        "freq": cur["freq"], "files": cur["files"],
    }), encoding="utf-8")

    loaded = graphindex.load(c.root)
    assert len(loaded.call_sites) == len(g.call_sites), "no edge may be lost"
    assert len(loaded.symbols) == len(g.symbols)
    assert all(s["bases"] == "" for s in loaded.symbols)
    assert all(cs["recv_type"] == "" for cs in loaded.call_sites)
    if version < 3:
        assert all(s["pmin"] == -1 and s["container"] == "" for s in loaded.symbols)
        assert all(cs["argc"] == -1 and cs["recv"] is None for cs in loaded.call_sites)


def test_legacy_v1_graph_still_loads(repo):
    """An existing .memo predates v2; it must keep working until the next index."""
    c = seed(repo, 5)
    g = graphindex.build(c.list_entries())
    (c.root / graphindex.GRAPH_FILE).write_text(json.dumps({
        "version": 1, "symbols": g.symbols, "call_sites": g.call_sites,
        "freq": g.freq, "files": g.files,
    }), encoding="utf-8")
    assert norm_graph(graphindex.load(c.root)) == norm_graph(g)


# --- incremental indexing ---------------------------------------------------

def run(*args):
    r = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert r.exit_code == 0, r.output
    return r


def test_reindex_reuses_unchanged_files(sample_repo):
    run("init")
    out = run("cache-dir", ".", "-r", "-q").output
    assert "reused" in out
    assert "Indexed 0 file(s)" in out


def test_reindex_reanalyzes_only_changed_files(sample_repo):
    run("init")
    (sample_repo / "src" / "Orders.cs").write_text(
        "public class OrderService { public int Total() { return 1; } }\n", encoding="utf-8")
    out = run("cache-dir", ".", "-r", "-q").output
    assert "Indexed 1 file(s)" in out


def test_force_reanalyzes_everything(sample_repo):
    run("init")
    out = run("cache-dir", ".", "-r", "-q", "--force").output
    assert "reused" not in out


def test_ignored_directories_are_pruned_not_enumerated(sample_repo):
    """Enumerating a pruned tree must skip it entirely, not visit-then-discard."""
    from memo.cli import _walk_sources
    from memo.ignore import load_ignore_spec

    junk = sample_repo / "node_modules" / "pkg" / "deep"
    junk.mkdir(parents=True)
    for i in range(5):
        (junk / f"j{i}.js").write_text("var x=1;\n", encoding="utf-8")
    (sample_repo / ".memoignore").write_text("node_modules/\n", encoding="utf-8")

    walked = _walk_sources(sample_repo, load_ignore_spec(sample_repo), True)
    assert not any("node_modules" in p.parts for p in walked)
    assert any(p.name == "Orders.cs" for p in walked), "real sources must still be found"


def test_pruning_does_not_change_what_gets_indexed(sample_repo):
    (sample_repo / "vendor").mkdir()
    (sample_repo / "vendor" / "lib.js").write_text("var y=2;\n", encoding="utf-8")
    run("init")
    with_vendor = {Path(e.path).name for e in Cache(find_cache_root()).list_entries()}
    assert "lib.js" in with_vendor, "nothing is pruned without an ignore rule"

    (sample_repo / ".memoignore").write_text("vendor/\n", encoding="utf-8")
    run("cache-dir", ".", "-r", "-q", "--force")
    after = {Path(e.path).name for e in Cache(find_cache_root()).list_entries()}
    assert "Orders.cs" in after


def test_tightening_memoignore_prunes_the_index(sample_repo):
    """Regression: a batch seeded its buffer from the existing index and only ever
    added, so newly ignored files stayed indexed forever. They were then invisible —
    still on disk, so neither "stale" nor "deleted", just permanent bloat."""
    junk = sample_repo / "vendor"
    junk.mkdir()
    for i in range(4):
        (junk / f"v{i}.js").write_text("var x=1;\n", encoding="utf-8")

    run("init")
    before = {Path(p).name for p in Cache(find_cache_root()).index_snapshot()}
    assert "v0.js" in before

    (sample_repo / ".memoignore").write_text("vendor/\n", encoding="utf-8")
    out = run("cache-dir", ".", "-r", "-q").output
    assert "pruned 4" in out, out

    after = {Path(p).name for p in Cache(find_cache_root()).index_snapshot()}
    assert not any(n.startswith("v") and n.endswith(".js") for n in after)
    assert "Orders.cs" in after, "real sources must survive pruning"


def test_orphaned_entry_files_are_reclaimed(sample_repo):
    """Regression: pruning dropped index paths but left their per-hash entry files, which
    are invisible to every command yet dominate .memo/ size (9,665 orphans / 78 MB
    against 541 live entries on one real repo)."""
    junk = sample_repo / "vendor"
    junk.mkdir()
    for i in range(5):
        (junk / f"v{i}.js").write_text(f"var x{i}=1;\n", encoding="utf-8")
    run("init")

    c = Cache(find_cache_root())
    before = len(list(c.cache_dir.glob("*.json")))
    assert before >= 6

    (sample_repo / ".memoignore").write_text("vendor/\n", encoding="utf-8")
    out = run("cache-dir", ".", "-r", "-q").output
    assert "reclaimed" in out, out

    c = Cache(find_cache_root())
    referenced = {r["hash"] for r in c.index_snapshot().values()}
    on_disk = {p.stem for p in c.cache_dir.glob("*.json")}
    assert on_disk <= referenced, f"orphans left behind: {on_disk - referenced}"


def test_gc_never_removes_a_referenced_entry(repo):
    c = seed(repo, 5)
    referenced = {r["hash"] for r in c.index_snapshot().values()}
    assert c.gc() == 0, "nothing is orphaned yet"
    assert {p.stem for p in c.cache_dir.glob("*.json")} == referenced
    assert len(c.list_entries()) == 5


def test_deleted_files_are_pruned(sample_repo):
    run("init")
    (sample_repo / "src" / "Orders.cs").unlink()
    run("cache-dir", ".", "-r", "-q")
    assert not any("Orders.cs" in p for p in Cache(find_cache_root()).index_snapshot())


def test_pruning_is_scoped_to_the_indexed_directory(sample_repo):
    """Indexing one subdirectory must never evict the rest of the repo."""
    other = sample_repo / "other"
    other.mkdir()
    (other / "Keep.cs").write_text("public class Keep { }\n", encoding="utf-8")
    run("init")
    assert any("Keep.cs" in p for p in Cache(find_cache_root()).index_snapshot())

    run("cache-dir", "src", "-r", "-q")  # index ONLY src/
    paths = Cache(find_cache_root()).index_snapshot()
    assert any("Keep.cs" in p for p in paths), "outside the indexed dir, must be untouched"
    assert any("Orders.cs" in p for p in paths)


def test_indexer_never_walks_its_own_cache(sample_repo):
    """.memo holds a JSON per file; walking it wastes time and can't yield symbols."""
    run("init")
    assert not any(".memo" in e.path.replace("\\", "/")
                   for e in Cache(find_cache_root()).list_entries())
