"""`memo arch` / `impact` / `changed` / `deadcode` — the repo-scale insight commands.

None of these had dedicated tests before this file: `deadcode()` got one incidental
assertion in test_confidence.py and `arch`/`impact`/`changed` and their renderers had
none at all, in a ~460-test suite. That gap is exactly how `render_arch` shipped with
`terse` accepted as a parameter and never read — `memo arch --terse` silently produced
the identical output as `memo arch`, undetected, because nothing ever called it.
"""

from __future__ import annotations

import subprocess

from memo import graphindex as gi
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.insights import (
    arch, changed, deadcode, impact, render_arch, render_changed, render_deadcode,
    render_impact,
)

CONTROLLER = """\
using System;
namespace Shop {
  public class OrdersController {
    public void Create(int a) { SaveOrder(a); }
    public void SaveOrder(int a) { Persist(a); }
    public void Persist(int a) { }
    public void Unreachable(int a) { }
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
    entries = c.list_entries()
    return c, gi.build(entries, cache_root=find_cache_root()), entries


def graph(repo):
    _c, g, entries = build(repo, {"src/OrdersController.cs": CONTROLLER})
    return g, entries


# --- arch -------------------------------------------------------------- #

def test_arch_reports_languages_and_symbol_kinds(repo):
    g, entries = graph(repo)
    a = arch(g, entries)
    assert a["languages"] == {"csharp": 1}
    assert a["symbol_kinds"].get("method") == 4


def test_arch_detects_entry_point_from_filename_pattern(repo):
    g, entries = graph(repo)
    a = arch(g, entries)
    names = {s["name"] for s in a["entry_points"]}
    assert "Create" in names  # OrdersController.cs matches the entry-point pattern


def test_arch_hotspots_ranked_by_indegree(repo):
    g, entries = graph(repo)
    a = arch(g, entries)
    names = [h["name"] for h in a["hotspots"]]
    assert "SaveOrder" in names and "Persist" in names
    assert names.index("Persist") < names.index("SaveOrder") \
        or a["hotspots"][names.index("Persist")]["callers"] >= \
           a["hotspots"][names.index("SaveOrder")]["callers"]


def test_render_arch_full_and_terse_are_not_identical(repo):
    """The regression test for the bug this file exists to catch: `terse` must
    change the output, not silently be accepted and ignored."""
    g, entries = graph(repo)
    a = arch(g, entries)
    full = render_arch(a, terse=False)
    terse = render_arch(a, terse=True)
    assert full != terse
    assert "# Architecture" in full
    assert "# Architecture" not in terse  # terse drops markdown headers entirely


def test_render_arch_terse_is_smaller(repo):
    g, entries = graph(repo)
    a = arch(g, entries)
    assert len(render_arch(a, terse=True)) < len(render_arch(a, terse=False))


def test_render_arch_terse_preserves_the_facts(repo):
    g, entries = graph(repo)
    a = arch(g, entries)
    terse = render_arch(a, terse=True)
    assert "Create" in terse
    assert "csharp" in terse
    assert str(a["files"]) in terse


# --- impact -------------------------------------------------------------- #

def test_impact_walks_transitive_callers(repo):
    g, _entries = graph(repo)
    r = impact(g, "Persist", max_depth=4)
    names = {a["name"] for a in r["affected"]}
    assert "SaveOrder" in names
    assert "Create" in names  # two hops out: Create -> SaveOrder -> Persist


def test_impact_reports_depth_correctly(repo):
    g, _entries = graph(repo)
    r = impact(g, "Persist", max_depth=4)
    depth_by_name = {a["name"]: a["depth"] for a in r["affected"]}
    assert depth_by_name["SaveOrder"] == 1
    assert depth_by_name["Create"] == 2


def test_impact_unmatched_symbol_has_no_seeds(repo):
    g, _entries = graph(repo)
    r = impact(g, "NoSuchMethodAnywhere")
    assert r["seeds"] == []
    assert r["affected"] == []


def test_render_impact_no_match_says_so(repo):
    g, _entries = graph(repo)
    r = impact(g, "NoSuchMethodAnywhere")
    out = render_impact(r)
    assert "No symbol matches" in out


def test_render_impact_terse_and_full_differ(repo):
    g, _entries = graph(repo)
    r = impact(g, "Persist")
    assert render_impact(r, terse=True) != render_impact(r, terse=False)


def test_render_impact_zero_affected_is_a_real_answer(repo):
    g, _entries = graph(repo)
    r = impact(g, "Unreachable")
    out = render_impact(r)
    assert "No production callers" in out


# --- deadcode -------------------------------------------------------------- #

DEAD_CODE_SRC = """\
using System;
namespace Shop {
  public class OrderService {
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
    public int Unreachable(int a) { return a; }
  }
}
"""


def dead_graph(repo):
    _c, g, entries = build(repo, {"src/OrderService.cs": DEAD_CODE_SRC})
    return g, entries


def test_deadcode_finds_uncalled_production_method(repo):
    # Not a controller-named file: deadcode() deliberately skips files matching the
    # entry-point pattern (they're assumed called externally), so a filename like
    # OrdersController.cs would hide this method from deadcode regardless of calls.
    g, _entries = dead_graph(repo)
    names = {d["name"] for d in deadcode(g)["dead"]}
    assert "Unreachable" in names
    assert "Add" not in names  # called by Total


def test_render_deadcode_terse_omits_the_caveat(repo):
    g, _entries = dead_graph(repo)
    d = deadcode(g)
    full = render_deadcode(d, terse=False)
    terse = render_deadcode(d, terse=True)
    assert "Heuristic" in full
    assert "Heuristic" not in terse
    assert "Unreachable" in terse


# --- changed -------------------------------------------------------------- #

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_changed_reports_blast_radius_of_a_diff(repo):
    (repo / "src").mkdir()
    f = repo / "src" / "OrdersController.cs"
    f.write_text(CONTROLLER, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    # Touch Persist — must show up as a changed, callable symbol with a blast radius.
    changed_src = CONTROLLER.replace("Persist(int a) { }", "Persist(int a) { /* x */ }")
    f.write_text(changed_src, encoding="utf-8")
    _c, g, _entries = build(repo, {"src/OrdersController.cs": changed_src})

    r = changed(g, "HEAD", max_depth=4)
    assert not r.get("error")
    symbols = {x["symbol"] for x in r["results"]}
    assert "Persist" in symbols


def test_render_changed_reports_git_error_cleanly(repo):
    g, _entries = graph(repo)
    r = changed(g, "not-a-real-ref-xyz", max_depth=2)
    assert r.get("error")
    out = render_changed(r)
    assert "git error" in out
