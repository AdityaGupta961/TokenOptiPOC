"""Confidence scoring must never change which edges exist.

memo's call graph is documented as recall-biased, and AGENTS.md tells agents to cite its
`file:line` without re-reading the file to confirm. Those two together make a dropped
edge the worst failure available: a real caller vanishes from a root-cause investigation
and nothing in the workflow will catch it. Confidence therefore only ever *orders* and
*annotates* results.

The first test here is the load-bearing one. Everything else in the feature can be wrong
and be fixed; if the edge set moves, the guarantee is gone.
"""

from __future__ import annotations

import pytest

from memo import graphindex as gi
from memo.analyzers.base import PF_EXTENSION, line_at, line_starts
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.graph import _ambiguity_note, find_callers, find_calls
from memo.graphindex import (
    RECV_BARE, RECV_EXPR, RECV_IDENT, RECV_SELF, TIER_CONTRA, TIER_EXACT, TIER_OK,
    TIER_STRONG, TIER_WEAK, _resolve_tier,
)
from memo.insights import deadcode
from memo.summarizer import summarize


def syms_of(text: str, language: str) -> list[dict]:
    return sorted((vars(s) for s in summarize(text, language).symbols if s.line),
                  key=lambda s: s["line"])


def dbl_of(syms: list[dict]) -> dict[int, str]:
    return {s["line"]: gi._short_name(s["name"]).lower() for s in syms}


def discovery_before_confidence(text: str, syms: list[dict], dbl: dict[int, str]) -> list:
    """The pre-confidence discovery logic, reproduced verbatim.

    Kept as a literal copy rather than a call into `_scan_file`, so that a future edit to
    the scanner is compared against what the graph used to contain, not against itself.
    """
    starts = line_starts(text)
    out = []
    for m in gi._CALL.finditer(text):
        callee = m.group(1)
        line = line_at(starts, m.start())
        if dbl.get(line) == callee.lower():
            continue
        encl = gi._enclosing(syms, line)
        if encl and encl.get("kind") == "interface":
            continue
        out.append((callee, line,
                    encl["name"] if encl else None,
                    encl["line"] if encl else 0))
    return out


SAMPLES = [
    ("csharp", """\
using System;
namespace Shop
{
    public class OrderService
    {
        private readonly Repo _repo;
        public int Total(int a)
        {
            var items = _repo.Load(a).Where(x => x.Ok).Select(Map).ToList();
            Log($"total={Fmt(a)}");
            return Discount(items.Count, 2);
        }
        public int Discount(int a, int b) { return a / b; }
        public int Map(object o) { return 0; }
        public void Log(string s) { }
        public string Fmt(int v) { return v.ToString(); }
    }
}
"""),
    ("vbnet", """\
Public Class LegacyHelper
    Public Sub Refresh()
        Dim arr(5) As Integer
        arr(0) = 5
        Load(1, 2)
        Me.Reset()
    End Sub
    Public Sub Load(ByVal a As Integer, ByVal b As Integer)
    End Sub
    Public Sub Reset()
    End Sub
End Class
"""),
    ("typescript", """\
export class UserCard {
  render(a: number) {
    const n = format(a, 2);
    return this.wrap(n);
  }
  wrap(s: string) { return s; }
}
function format(v: number, p: number) { return String(v); }
"""),
    ("python", """\
class Widget:
    def render(self, a):
        return self.wrap(fmt(a, 2))

    def wrap(self, s):
        return s

def fmt(v, p):
    return str(v)
"""),
]


@pytest.mark.parametrize("language,text", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_edge_set_is_identical_to_pre_confidence_discovery(language, text):
    syms = syms_of(text, language)
    dbl = dbl_of(syms)
    want = discovery_before_confidence(text, syms, dbl)
    got = [tuple(c[:4]) for c in gi._scan_file(text, syms, dbl, language)["cands"]]
    assert got == want, "confidence capture must not add, drop or reorder a single edge"


def test_interpolated_string_call_still_produces_an_edge():
    """Pins the reason discovery stays on raw text.

    `$"{Fmt(a)}"` yields an edge for `Fmt` today. Making the scanner string-aware so the
    argument counter could skip literals would silently delete it — losing the edge to
    gain a count is the wrong trade in both directions.
    """
    text = SAMPLES[0][1]
    syms = syms_of(text, "csharp")
    cands = gi._scan_file(text, syms, dbl_of(syms), "csharp")["cands"]
    assert any(c[0] == "Fmt" for c in cands)


def test_a_call_inside_a_comment_still_produces_an_edge():
    text = "class A {\n  void Go() {\n    // Helper(1);\n  }\n  void Helper(int x) {}\n}\n"
    syms = syms_of(text, "csharp")
    cands = gi._scan_file(text, syms, dbl_of(syms), "csharp")["cands"]
    assert any(c[0] == "Helper" for c in cands), \
        "comment filtering belongs to discovery, which this change does not touch"


def test_every_call_site_carries_a_receiver_and_arity_slot():
    text = SAMPLES[0][1]
    syms = syms_of(text, "csharp")
    for c in gi._scan_file(text, syms, dbl_of(syms), "csharp")["cands"]:
        assert len(c) == 8
        assert isinstance(c[5], int)  # recv_kind
        assert isinstance(c[6], int)  # argc, -1 when unknown
        assert isinstance(c[7], str)  # recv_type, "" when unknown


# --- tiering ----------------------------------------------------------------
#
# The scenario below is the one this whole feature exists for: two classes each define
# `Save`, so a name-based edge cannot say which a call reaches, and the legacy tie break
# was alphabetical path order.

AMBIGUOUS = {
    "Notes.cs": """\
namespace App
{
    public class NoteStore
    {
        public int Save(string body) { return 1; }
        public int Reload() { return Save("x"); }
    }
}
""",
    "Audit.cs": """\
namespace App
{
    public class AuditStore
    {
        public int Save(string who, string what) { return 2; }
    }
}
""",
    "Api.cs": """\
namespace App
{
    public class NoteApi
    {
        private NoteStore notes;
        private AuditStore audit;
        public void PostNote(string b)
        {
            notes.Save(b);
            audit.Save("me", b);
        }
    }
}
""",
    # A receiver with no recoverable declared type: `var` on a method result. Keeps the
    # arity-only and no-evidence paths reachable now that declared types resolve the rest.
    "Untyped.cs": """\
namespace App
{
    public class NoteRelay
    {
        public void Relay(string b)
        {
            var store = Resolve();
            store.Save(b);
            var other = Resolve();
            other.Save("me", b);
        }
        public object Resolve() { return null; }
    }
}
""",
}


@pytest.fixture
def ambiguous_repo(repo):
    """A built graph over AMBIGUOUS, plus the Cache."""
    src = repo / "src"
    src.mkdir()
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for name, body in AMBIGUOUS.items():
            p = src / name
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return gi.build(c.list_entries()), c


def site(graph, callee: str, in_file: str, argc: int | None = None) -> dict:
    """The call site for `callee` inside `in_file`."""
    for cs in graph.call_sites:
        if cs["callee"] == callee and cs["caller_path"].endswith(in_file):
            if argc is None or cs["argc"] == argc:
                return cs
    raise AssertionError(f"no call site for {callee} in {in_file}")


def definition(graph, container: str, name: str) -> dict:
    return next(s for s in graph.symbols
                if s["name"] == name and s["container"] == container)


def test_declared_receiver_type_resolves_exactly(ambiguous_repo):
    """`private NoteStore notes;` is what makes `notes.Save(b)` answerable."""
    graph, _ = ambiguous_repo
    cs = site(graph, "Save", "Api.cs", argc=1)
    assert cs["recv"] == "notes" and cs["recv_kind"] == RECV_IDENT
    assert cs["recv_type"] == "NoteStore"
    tier = lambda d: _resolve_tier(cs, d, "NoteApi", graph.type_names, graph.implementors)
    assert tier(definition(graph, "NoteStore", "Save")) == TIER_EXACT
    assert tier(definition(graph, "AuditStore", "Save")) == TIER_CONTRA
    assert graph._best_def("save", cs, "NoteApi")["container"] == "NoteStore"


def test_an_untyped_receiver_falls_back_to_arity(ambiguous_repo):
    """No declared type, so only the argument count can separate the candidates."""
    graph, _ = ambiguous_repo
    cs = site(graph, "Save", "Untyped.cs", argc=1)
    assert cs["recv"] == "store" and not cs["recv_type"]
    tier = lambda d: _resolve_tier(cs, d, "NoteRelay", graph.type_names,
                                   graph.implementors)
    assert tier(definition(graph, "NoteStore", "Save")) == TIER_OK
    assert tier(definition(graph, "AuditStore", "Save")) == TIER_CONTRA


def test_best_def_prefers_the_arity_match_over_path_order(ambiguous_repo):
    """`Audit.cs` sorts before `Notes.cs`, so the legacy key picked the wrong Save."""
    graph, _ = ambiguous_repo
    cs = site(graph, "Save", "Api.cs", argc=2)
    got = graph._best_def("save", cs, "NoteApi")
    assert got["container"] == "AuditStore", "two arguments can only reach AuditStore.Save"


def test_self_receiver_resolves_within_its_own_class(ambiguous_repo):
    graph, _ = ambiguous_repo
    cs = site(graph, "Save", "Notes.cs")
    assert graph._best_def("save", cs, "NoteStore")["container"] == "NoteStore"


def test_contradicting_arity_never_removes_an_edge(ambiguous_repo):
    graph, _ = ambiguous_repo
    result = find_callers(graph, "Save")
    callers = {c["caller"] for c in result["callers"]}
    assert {"PostNote", "Reload"} <= callers, \
        "every caller must survive, whatever its arity agreement"


def test_contradicted_groups_sort_last_but_are_still_present(ambiguous_repo):
    graph, _ = ambiguous_repo
    rows = graph.callers(gi._collapse("Save"))
    tiers = [r["tier"] for r in rows]
    assert tiers == sorted(tiers, reverse=True), "higher tiers must come first"
    assert {r["caller"] for r in rows} == {"PostNote", "Reload", "Relay"}, \
        "every calling method must appear, whatever its tier"


def test_best_def_falls_back_to_legacy_order_when_evidence_is_weak(ambiguous_repo):
    """An expression receiver with no count must not move the answer."""
    graph, _ = ambiguous_repo
    blind = {"caller_path": "", "caller_line": 0, "recv": None,
             "recv_kind": RECV_EXPR, "argc": -1}
    assert graph._best_def("save", blind, "") == graph._best_def("save")


def test_weak_evidence_cannot_outrank_the_legacy_key(ambiguous_repo):
    graph, _ = ambiguous_repo
    for kind in (RECV_BARE, RECV_EXPR):
        cs = {"caller_path": "", "caller_line": 0, "recv": "notes",
              "recv_kind": kind, "argc": -1}
        assert graph._best_def("save", cs, "NoteApi") == graph._best_def("save"), \
            f"receiver kind {kind} carries no information and must change nothing"


def test_deadcode_ignores_confidence_entirely(ambiguous_repo):
    """Tier-filtering here would invent dead code, the worst output memo could give."""
    graph, _ = ambiguous_repo
    names = {d["name"] for d in deadcode(graph)["dead"]}
    assert "Save" not in names, "both Saves are called; neither may be reported dead"


def test_ambiguity_note_survives_identical_arity_overloads(repo):
    """Arity cannot separate same-arity overloads, so the warning must stay."""
    src = repo / "src"
    src.mkdir()
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    files = {
        "A.cs": """\
namespace N
{
    public class A
    {
        public int Run(int x) { return 1; }
    }
}
""",
        "B.cs": """\
namespace N
{
    public class B
    {
        public int Run(int x) { return 2; }
    }
}
""",
        # The receiver has no recoverable declared type, so arity is the only signal —
        # and both candidates take one argument.
        "C.cs": """\
namespace N
{
    public class C
    {
        public void Go() { var t = Pick(); t.Run(1); }
        public object Pick() { return null; }
    }
}
""",
    }
    with c.batch():
        for name, body in files.items():
            p = src / name
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    graph = gi.build(c.list_entries())

    note = _ambiguity_note(find_callers(graph, "Run"))
    assert note is not None
    assert "matches 2 definitions" in note
    assert "resolves to" not in note, "a count cannot settle a same-arity overload"


def test_arity_resolves_by_elimination_not_only_by_promotion(ambiguous_repo):
    """The commonest way arity helps: it rules candidates out rather than promoting one.

    `audit.Save("me", b)` gives both Saves tier OK/CONTRA in isolation — neither reaches
    STRONG — but only one can take two arguments. Judging candidates one at a time cannot
    see that; comparing them can.
    """
    graph, _ = ambiguous_repo
    cs = site(graph, "Save", "Untyped.cs", argc=2)  # untyped receiver: arity is all we have
    tiers = {id(s): _resolve_tier(cs, s, "NoteRelay", graph.type_names, graph.implementors)
             for s in graph.sym_by_name["save"]}
    assert max(tiers.values()) < TIER_STRONG, "no candidate is promoted on its own"
    assert graph._best_def("save", cs, "NoteRelay")["container"] == "AuditStore"


def test_elimination_needs_something_eliminated(ambiguous_repo):
    """One survivor out of one is a unique name, not evidence."""
    graph, _ = ambiguous_repo
    blind = {"caller_path": "", "caller_line": 0, "recv": None,
             "recv_kind": RECV_EXPR, "argc": -1}
    assert graph._best_def("save", blind, "") == graph._best_def("save")


def test_a_variable_named_after_its_type_does_not_score_exact():
    """Regression: `a.Run(1)` scored EXACT against class `A`.

    Comparing a receiver *variable* to a *type* name case-insensitively turns a naming
    convention into certainty. It is usually right and occasionally very wrong, which is
    the worst combination for something agents are told not to re-verify.
    """
    cs = {"caller_path": "C.cs", "caller_line": 1, "recv": "a",
          "recv_kind": RECV_IDENT, "argc": 1}
    sym = {"container": "A", "path": "A.cs", "pmin": 1, "pmax": 1, "pflags": 0}
    assert _resolve_tier(cs, sym, "C", frozenset({"A"})) == TIER_OK


def test_a_static_call_on_a_real_type_scores_exact():
    cs = {"caller_path": "C.cs", "caller_line": 1, "recv": "Mapper",
          "recv_kind": RECV_IDENT, "argc": 1}
    sym = {"container": "Mapper", "path": "M.cs", "pmin": 1, "pmax": 1, "pflags": 0}
    assert _resolve_tier(cs, sym, "C", frozenset({"Mapper"})) == TIER_EXACT
    # ...but only when that name is actually a known type in this repo.
    assert _resolve_tier(cs, sym, "C", frozenset()) == TIER_OK


def test_a_prefix_query_spanning_two_names_keeps_the_warning(repo):
    """Regression: `callers HandleException` claimed resolution to HandleExceptionAsync.

    The prefix query matched two different methods. One site resolved confidently — to
    the *other* name — and counting only confident sites turned a useful warning into a
    wrong reassurance. A query spanning several names needs narrowing, not resolving.
    """
    src = repo / "src"
    src.mkdir()
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    files = {
        "Proc.cs": """\
namespace N
{
    public class Proc
    {
        public void HandleException(string m) { }
    }
}
""",
        "Middle.cs": """\
namespace N
{
    public class Middle
    {
        public void HandleExceptionAsync(string m) { }
        public void Invoke(string m) { HandleExceptionAsync(m); }
    }
}
""",
    }
    with c.batch():
        for name, body in files.items():
            p = src / name
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    graph = gi.build(c.list_entries())

    assert graph.unanimous_target(gi._collapse("HandleException")) is None
    note = _ambiguity_note(find_callers(graph, "HandleException"))
    assert note is not None and "matches 2 definitions" in note


def test_one_unresolved_site_keeps_the_warning(ambiguous_repo):
    """"Every confident site agrees" is not the same claim as "every site agrees"."""
    graph, _ = ambiguous_repo
    # `notes.Save(b)` and `audit.Save(..)` are separated by arity, not by a confident
    # receiver, so nothing here reaches STRONG and the warning must stand.
    assert graph.unanimous_target(gi._collapse("Save")) is None


def test_extension_method_arity_is_not_a_contradiction():
    cs = {"caller_path": "x", "caller_line": 0, "recv": "svc",
          "recv_kind": RECV_IDENT, "argc": 1}
    sym = {"container": "Extensions", "path": "y", "pmin": 2, "pmax": 2,
           "pflags": PF_EXTENSION}
    assert _resolve_tier(cs, sym) != TIER_CONTRA


def test_unknown_arity_is_not_a_contradiction():
    cs = {"caller_path": "x", "caller_line": 0, "recv": None,
          "recv_kind": RECV_BARE, "argc": -1}
    sym = {"container": "", "path": "y", "pmin": 3, "pmax": 3, "pflags": 0}
    assert _resolve_tier(cs, sym) == TIER_WEAK


def test_argument_counts_are_captured_at_real_call_sites():
    text = SAMPLES[0][1]
    syms = syms_of(text, "csharp")
    cands = gi._scan_file(text, syms, dbl_of(syms), "csharp")["cands"]
    by_name = {c[0]: c for c in cands}
    assert by_name["Discount"][6] == 2
    assert by_name["ToList"][6] == 0


def test_edge_count_survives_a_full_build(ambiguous_repo):
    """End to end: scoring is layered on, so the graph must be no smaller for it."""
    graph, _ = ambiguous_repo
    assert len(graph.call_sites) > 0
    assert all("tier" not in cs for cs in graph.call_sites), \
        "tiers are computed per query, not baked into the stored edge"


def test_a_call_inside_an_interpolated_string_keeps_its_edge_but_not_its_count():
    """The two halves of the split, in one assertion.

    Discovery reads raw text, so `$"total={Fmt(a)}"` keeps its `Fmt` edge. Measurement
    skips string literals, so it reports no count for it. Unknown is the honest answer and
    scores nothing; a guessed count would score as evidence.
    """
    text = SAMPLES[0][1]
    syms = syms_of(text, "csharp")
    cands = gi._scan_file(text, syms, dbl_of(syms), "csharp")["cands"]
    fmt = next(c for c in cands if c[0] == "Fmt")
    assert fmt[6] == -1
