"""Output determinism and stderr discipline.

Both properties exist for the same reason: memo's output is consumed by an agent
that re-sends it on every subsequent model turn. Anything printed on a healthy run
is paid for repeatedly, and anything that varies between identical runs defeats the
prompt cache that would otherwise serve the second one for a tenth of the price.
"""

from __future__ import annotations

from click.testing import CliRunner

from memo.cache import Cache
from memo.cli import main
from memo.config import find_cache_root


def run(*args, env=None):
    r = CliRunner(env=env).invoke(main, list(args), catch_exceptions=False)
    assert r.exit_code == 0, r.output
    return r


# --- stderr discipline ------------------------------------------------------

def test_healthy_graph_command_prints_nothing_to_stderr(sample_repo):
    """A run with nothing wrong should cost zero diagnostic tokens."""
    run("init")
    r = run("callers", "Add")
    assert "memo: cache" not in r.stderr
    assert r.stderr.strip() == ""


def test_memo_verbose_restores_the_banner(sample_repo):
    run("init")
    r = run("callers", "Add", env={"MEMO_VERBOSE": "1"})
    assert "memo: cache" in r.stderr


def test_stale_graph_still_warns_without_verbose(sample_repo):
    """The safety property the banner was protecting must survive its removal.

    A graph that no longer matches the cache is the one condition that must never be
    silent, verbose or not. Asserted on the property rather than the wording — the
    message itself is owned by tests/test_staleness.py, which covers *which* conditions
    are detected.
    """
    run("init")
    (sample_repo / "extra.py").write_text("def solo():\n    return 1\n", encoding="utf-8")
    run("cache", "extra.py")  # grows the cache but not the graph

    r = run("callers", "Add")
    assert "warning:" in r.stderr
    assert "cache-dir" in r.stderr, "the warning must say how to fix it"


# --- byte-for-byte reproducibility -----------------------------------------

def test_repeated_identical_invocations_are_byte_identical(sample_repo):
    run("init")
    for cmd in (("callers", "Add"), ("arch",), ("map", "Add"), ("deadcode",)):
        first = run(*cmd).output
        second = run(*cmd).output
        assert first == second, f"{cmd[0]} output is not reproducible"


# --- the ordering guarantee everything downstream leans on -----------------

def test_list_entries_is_sorted_by_path(sample_repo):
    run("init")
    entries = Cache(find_cache_root()).list_entries()
    paths = [e.path for e in entries]
    assert paths == sorted(paths)


def test_ordering_survives_an_incremental_single_file_cache(sample_repo):
    """The real-world drift: `cache <file>` appends to index.json.

    Indexing writes index.json in sorted order, but a later single-file `cache`
    appends its path at the end. Consumers then sort with keys that tie often, and
    because Python's sort is stable those ties resolve differently depending on
    indexing history rather than repo content. `aaa.py` sorts first but is indexed
    last, so index order and sorted order disagree here by construction.
    """
    run("init")
    (sample_repo / "aaa.py").write_text("def zzz():\n    return 1\n", encoding="utf-8")
    run("cache", "aaa.py")

    paths = [e.path for e in Cache(find_cache_root()).list_entries()]
    assert paths == sorted(paths)
    assert paths[0].endswith("aaa.py"), "appended path did not sort to the front"


def test_manifest_fast_path_matches_slow_path_order(sample_repo):
    """list_entries has two code paths; they must not disagree on order.

    The manifest is the fast path and is written from the slow path, but a manifest
    persisted by an older memo predates the ordering guarantee.
    """
    run("init")
    c = Cache(find_cache_root())
    from_slow = [e.path for e in c._read_all_entries(c._load_index())]
    from_cached = [e.path for e in c.list_entries()]
    assert from_slow == from_cached
