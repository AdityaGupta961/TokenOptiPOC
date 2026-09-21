"""`memo trace-stack` — resolving pasted exception frames to current coordinates.

Most of these are parser tests, because the parser is where real traces defeat naive
implementations: .NET async and lambda frames name compiler-generated wrappers rather
than the method the developer wrote, and Windows paths carry a drive-letter colon that
a `:line:col` pattern will happily mis-split.

The drift tests are the ones that matter most. A frame whose line number is stale but
whose symbol name is good is the fastest available route to a confidently wrong
hypothesis, and nothing downstream catches it.
"""

from __future__ import annotations

from memo import graphindex as gi
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.stacktrace import parse_frames, render_stack, resolve_stack

PROD = """\
using System;
namespace Shop.Api {
  public class OrderService {
    public int SaveAsync(Order o)
    {
      var x = 1;
      return x;
    }
  }
}
"""


def graph(repo):
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    p = repo / "src" / "OrderService.cs"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(PROD, encoding="utf-8")
    with c.batch():
        c.store(_build_entry(p, language_for(p, exts)))
    return gi.build(c.list_entries()), p


def one(text):
    f = parse_frames(text)
    assert len(f) == 1, f"expected 1 frame, got {len(f)}: {f}"
    return f[0]


# --- .NET parsing -----------------------------------------------------------

def test_dotnet_frame_with_location():
    f = one(r"   at Shop.Api.OrderService.SaveAsync(Order o) in C:\src\OrderService.cs:line 42")
    assert f["method"] == "SaveAsync"
    assert f["segments"] == ["Shop", "Api", "OrderService", "SaveAsync"]
    assert f["line"] == 42
    assert f["path"] == r"C:\src\OrderService.cs"


def test_dotnet_frame_without_location():
    f = one("   at Shop.Api.OrderService.SaveAsync(Order o)")
    assert f["method"] == "SaveAsync"
    assert f["line"] is None


def test_async_state_machine_is_unwrapped():
    """Any trace through `async` code is otherwise all MoveNext and resolves to nothing."""
    f = one("   at Shop.Api.OrderService+<SaveAsync>d__12.MoveNext()")
    assert f["method"] == "SaveAsync"
    assert "OrderService" in f["segments"]
    assert "MoveNext" not in f["segments"]


def test_lambda_closure_is_unwrapped():
    f = one("   at Shop.Api.OrderService.<>c__DisplayClass0_0.<SaveAsync>b__0()")
    assert f["method"] == "SaveAsync"


def test_generic_arity_is_stripped():
    f = one("   at System.Collections.Generic.List`1.ForEach(Action`1 action)")
    assert f["method"] == "ForEach"


# --- other languages --------------------------------------------------------

def test_python_traceback_frame():
    f = one('  File "/app/svc.py", line 42, in save')
    assert (f["method"], f["line"], f["path"]) == ("save", 42, "/app/svc.py")


def test_js_frame():
    f = one("    at save (/app/svc.js:42:10)")
    assert (f["method"], f["line"]) == ("save", 42)


def test_js_frame_with_windows_drive_letter():
    """A drive-letter colon must not be mistaken for the `:line:col` delimiter.

    Characterisation rather than regression — the pattern always handled this. It is
    pinned because the obvious "tighten the path class" edit breaks it silently, and
    the whole target stack is Windows.
    """
    f = one(r"    at peek (E:\repos\proj\src\svc.ts:334:5)")
    assert f["method"] == "peek"
    assert f["line"] == 334
    assert f["path"] == r"E:\repos\proj\src\svc.ts"


def test_noise_lines_are_skipped_not_fatal():
    frames = parse_frames(
        "System.NullReferenceException: Object reference not set\n"
        "   --- End of stack trace from previous location ---\n"
        "   at Shop.Api.OrderService.SaveAsync(Order o)\n"
    )
    assert [f["method"] for f in frames] == ["SaveAsync"]


# --- resolution -------------------------------------------------------------

def test_resolves_to_current_coordinates(repo):
    g, p = graph(repo)
    r = resolve_stack(g, "   at Shop.Api.OrderService.SaveAsync(Order o)")
    f = r["frames"][0]
    assert f["status"] == "project"
    assert f["path"] == str(p.resolve())
    assert f["line"] == 4


def test_framework_frames_are_marked_external(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "   at System.Linq.Enumerable.Select(IEnumerable source)\n"
                         "   at Microsoft.AspNetCore.Mvc.Invoker.Next(State next)")
    assert [f["status"] for f in r["frames"]] == ["external", "external"]
    assert r["external"] == 2


def test_node_modules_path_is_external(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "    at handle (/app/node_modules/express/lib/router.js:12:3)")
    assert r["frames"][0]["status"] == "external"


def test_unknown_symbol_is_reported_unresolved_not_guessed(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "   at Shop.Api.NoSuchThing.Missing(Order o)")
    assert r["frames"][0]["status"] == "unresolved"
    assert r["frames"][0]["path"] is None


def test_start_here_is_the_topmost_project_frame(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "   at System.Linq.Enumerable.Select(IEnumerable s)\n"
                         "   at Shop.Api.OrderService.SaveAsync(Order o)")
    assert r["start_here"]["method"] == "SaveAsync"
    assert r["start_here"]["n"] == 2


# --- build drift: the load-bearing behaviour ---------------------------------

def test_drift_is_flagged_when_the_trace_line_is_outside_the_symbol(repo):
    g, p = graph(repo)
    r = resolve_stack(
        g, f"   at Shop.Api.OrderService.SaveAsync(Order o) in {p}:line 9999")
    f = r["frames"][0]
    assert f["drift"] is True
    assert r["drift"] == [1]
    out = render_stack(r)
    assert "Build drift" in out
    assert "Trust the symbol, not the trace's line" in out


def test_no_drift_when_the_trace_line_is_inside_the_symbol(repo):
    g, p = graph(repo)
    r = resolve_stack(
        g, f"   at Shop.Api.OrderService.SaveAsync(Order o) in {p}:line 6")
    assert r["frames"][0]["drift"] is False
    assert r["drift"] == []
    assert "Build drift" not in render_stack(r)


def test_a_frame_with_no_line_number_cannot_drift(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "   at Shop.Api.OrderService.SaveAsync(Order o)")
    assert r["frames"][0]["drift"] is False


# --- rendering --------------------------------------------------------------

def test_unrecognised_input_explains_the_supported_shapes(repo):
    g, _ = graph(repo)
    out = render_stack(resolve_stack(g, "this is not a stack trace at all"))
    assert "No stack frames recognised" in out
    assert ".NET" in out and "Python" in out


def test_terse_mode_is_one_line_per_frame(repo):
    g, _ = graph(repo)
    r = resolve_stack(g, "   at System.Linq.Enumerable.Select(IEnumerable s)\n"
                         "   at Shop.Api.OrderService.SaveAsync(Order o)")
    body = [ln for ln in render_stack(r, terse=True).strip().splitlines()]
    assert len(body) == 3  # header + 2 frames
    assert "Build drift" not in "\n".join(body)
