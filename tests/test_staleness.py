"""Staleness detection for the persisted graph.

The load-bearing test is `test_in_place_edit_is_detected`. The generated rule files tell
agents to cite memo's `file:line` *without* re-reading to confirm, so a stale line number
is not caught by anything downstream — the agent either re-reads (cancelling the whole
saving) or edits the wrong place and redoes the work. Silence here is the single most
expensive failure mode memo has.

The count-only check that preceded this could not see an edit that left the file count
unchanged, which is the common case rather than the corner case.
"""

from __future__ import annotations

from click.testing import CliRunner

from memo import graphindex as gi
from memo.cache import Cache
from memo.cli import _graph_staleness, main
from memo.config import find_cache_root

ORIG = """\
using System;
namespace Shop {
  public class OrderService {
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
  }
}
"""

# Same file count, same symbol names — only the line numbers move.
SHIFTED = """\
using System;
// a comment inserted at the top, pushing every symbol down four lines
// second line
// third line
namespace Shop {
  public class OrderService {
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
  }
}
"""


def run(*args, env=None):
    r = CliRunner(env=env).invoke(main, list(args), catch_exceptions=False)
    assert r.exit_code == 0, r.output
    return r


def seed(repo, body=ORIG):
    src = repo / "src"
    src.mkdir(exist_ok=True)
    (src / "Orders.cs").write_text(body, encoding="utf-8")
    return src / "Orders.cs"


# --- fingerprint semantics --------------------------------------------------

def test_fingerprint_is_stable_for_the_same_inputs(repo):
    seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    c = Cache(find_cache_root())
    assert gi.fingerprint_of(c.list_entries()) == gi.fingerprint_of(c.list_entries())


def test_fingerprint_is_order_independent(repo):
    """It digests a sorted set, so entry order must not matter."""
    seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    entries = Cache(find_cache_root()).list_entries()
    assert gi.fingerprint_of(entries) == gi.fingerprint_of(list(reversed(entries)))


def test_fingerprint_changes_when_content_changes(repo):
    p = seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    before = gi.fingerprint_of(Cache(find_cache_root()).list_entries())

    p.write_text(SHIFTED, encoding="utf-8")
    run("cache-dir", ".", "-r", "-q")
    after = gi.fingerprint_of(Cache(find_cache_root()).list_entries())
    assert before != after


def test_graph_records_the_fingerprint_it_was_built_from(repo):
    seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    c = Cache(find_cache_root())
    g = gi.load(find_cache_root())
    assert g.fingerprint
    assert g.fingerprint == gi.fingerprint_of(c.list_entries())
    assert _graph_staleness(g, c.list_entries()) is None


# --- the case the count-only check could not see ----------------------------

def test_in_place_edit_is_detected(repo):
    """Same file count, moved lines. The old count-only guard was silent here."""
    p = seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    g = gi.load(find_cache_root())          # graph as built
    p.write_text(SHIFTED, encoding="utf-8")  # edit in place
    run("cache", str(p))                     # re-cache the one file: count unchanged

    c = Cache(find_cache_root())
    assert len(c.list_entries()) == g.files, "count must be unchanged for this to be the real case"
    assert _graph_staleness(g, c.list_entries()) is not None


def test_query_warns_after_an_in_place_edit(repo):
    p = seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    p.write_text(SHIFTED, encoding="utf-8")
    run("cache", str(p))

    r = run("callers", "Add")
    assert "warning:" in r.stderr
    assert "lines that have moved" in r.stderr


def test_status_reports_the_same_verdict_as_the_query_warning(repo):
    """status and the live warning must never disagree — they share one function."""
    p = seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    p.write_text(SHIFTED, encoding="utf-8")
    run("cache", str(p))

    import json
    report = json.loads(run("status", "--json").output)
    assert report["graph_matches_cache"] is False
    assert report["graph_stale_reason"]
    assert report["graph_fingerprinted"] is True


def test_healthy_index_warns_about_nothing(repo):
    seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    r = run("callers", "Add")
    assert r.stderr.strip() == ""


# --- backward compatibility -------------------------------------------------

def test_graph_without_a_fingerprint_falls_back_to_the_file_count(repo):
    """An older graph.json has no fingerprint; absent must not read as mismatched."""
    seed(repo)
    run("init", "--no-index")
    run("cache-dir", ".", "-r", "-q")
    c = Cache(find_cache_root())
    entries = c.list_entries()

    g = gi.load(find_cache_root())
    g.fingerprint = ""                      # simulate a pre-fingerprint graph
    assert _graph_staleness(g, entries) is None, "absent fingerprint must not warn"

    g.files = len(entries) + 5              # count disagrees -> old check still fires
    assert _graph_staleness(g, entries) is not None


def test_missing_graph_is_not_reported_as_stale(repo):
    seed(repo)
    run("init", "--no-index")
    assert _graph_staleness(None, []) is None
