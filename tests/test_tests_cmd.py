"""`memo tests` — the test methods that reach a symbol.

The load-bearing case is depth 2. A test rarely calls the symbol under change
directly; the usual shape is `TestMethod -> ProductionService -> target`. A version of
this feature that merely inverted `impact`'s test filter would find only the direct
callers and silently under-report, which for a "what should I run" answer is worse
than returning nothing.
"""

from __future__ import annotations

from memo import graphindex as gi
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.insights import covering_tests, render_tests

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

TESTS = """\
using System;
namespace Shop.Tests {
  public class OrderServiceTests {
    public void Add_directly() { var s = new OrderService(); s.Add(1, 2); }
    public void Total_indirectly() { var s = new OrderService(); s.Total(1, 2); }
  }
}
"""


def build(repo, files: dict[str, str]):
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for rel, body in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return gi.build(c.list_entries())


def graph(repo):
    return build(repo, {"src/OrderService.cs": PROD,
                        "tests/OrderServiceTests.cs": TESTS})


def names(r):
    return {t["name"] for t in r["tests"]}


def depth_of(r, name):
    return next(t["depth"] for t in r["tests"] if t["name"] == name)


# --- traversal --------------------------------------------------------------

def test_finds_a_direct_test_caller(repo):
    r = covering_tests(graph(repo), "Add")
    assert "Add_directly" in names(r)
    assert depth_of(r, "Add_directly") == 1


def test_finds_a_test_that_reaches_the_symbol_through_production_code(repo):
    """The reason this is not just impact() with a flipped boolean."""
    r = covering_tests(graph(repo), "Add")
    assert "Total_indirectly" in names(r), "indirect coverage was missed"
    assert depth_of(r, "Total_indirectly") == 2


def test_records_the_hop_it_came_through(repo):
    r = covering_tests(graph(repo), "Add")
    via = next(t["via"] for t in r["tests"] if t["name"] == "Total_indirectly")
    assert via == "Total"


def test_production_callers_are_not_reported_as_tests(repo):
    r = covering_tests(graph(repo), "Add")
    assert "Total" not in names(r)


def test_each_test_appears_once_at_its_nearest_depth(repo):
    """Add is reachable from Add_directly at d1 and would also surface deeper."""
    r = covering_tests(graph(repo), "Add")
    listed = [t["name"] for t in r["tests"]]
    assert len(listed) == len(set(listed))


def test_depth_limit_is_honoured(repo):
    r = covering_tests(graph(repo), "Add", max_depth=1)
    assert "Add_directly" in names(r)
    assert "Total_indirectly" not in names(r)


# --- the two different kinds of empty ---------------------------------------

def test_unknown_symbol_says_the_query_is_wrong(repo):
    r = covering_tests(graph(repo), "NoSuchSymbolAnywhere")
    assert r["seeds"] == []
    assert "No symbol matches" in render_tests(r)


def test_known_symbol_with_no_reaching_test_reads_as_a_finding(repo):
    """A coverage gap is an answer, not an empty result."""
    r = covering_tests(graph(repo), "Lonely")
    assert r["seeds"] != []
    assert r["tests"] == []
    out = render_tests(r)
    assert out.startswith("NONE")
    assert "coverage gap" in out


# --- the runnable command is the deliverable --------------------------------

def test_dotnet_filter_is_generated_for_csharp(repo):
    r = covering_tests(graph(repo), "Add")
    cmd = r["command"]
    assert cmd.startswith("dotnet test --filter")
    assert "FullyQualifiedName~" in cmd
    # Container-qualified: a bare method name is ambiguous across test classes.
    assert "OrderServiceTests.Add_directly" in cmd


def test_pytest_filter_is_generated_for_python(repo):
    g = build(repo, {
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    })
    r = covering_tests(g, "add")
    assert r["command"].startswith("python -m pytest -k")
    assert "test_add" in r["command"]


def test_no_command_when_nothing_was_found(repo):
    assert covering_tests(graph(repo), "Lonely")["command"] == ""


def test_caveat_is_dropped_in_terse_mode(repo):
    r = covering_tests(graph(repo), "Add")
    assert "name-based" in render_tests(r)
    assert "name-based" not in render_tests(r, terse=True)
