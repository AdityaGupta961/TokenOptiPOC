"""Declared receiver types — the signal that separates same-name, same-arity methods.

On a 350-file C# service, 90% of ambiguous callees were pairs like `CaseProcessor.Save`
and `ICaseProcessor.Save`: identical name, identical arity, differing only by the type
they hang off. Neither the argument count nor the receiver's *name* can choose between
them. The receiver's declared type can, and C# and VB write it down in source.

The extraction is heuristic and file-scoped, so the governing rule is the same as
everywhere else here: when two declarations disagree, claim nothing.
"""

from __future__ import annotations

import pytest

from memo import graphindex as gi
from memo.analyzers.base import base_types, declared_types
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions
from memo.graphindex import RECV_IDENT, TIER_EXACT, _resolve_tier, _satisfies

# --- extraction -------------------------------------------------------------

CS = """\
namespace N
{
    public class Svc : BaseSvc, ICaseProcessor
    {
        private readonly ICaseProcessor _processor;
        public IFoo Foo { get; set; }
        private List<Order> _orders;
        [Required] public string Name { get; set; }
        public IActionResult Create(OrderDto dto)
        {
            var local = new Helper();
            ICaseProcessor other = Resolve();
            return Ok();
        }
        public Task<IActionResult> CreateAsync(OrderDto dto) => null;
    }
}
"""


def test_csharp_fields_properties_params_and_locals():
    got = declared_types(CS, "csharp")
    assert got["_processor"] == "ICaseProcessor"  # field
    assert got["Foo"] == "IFoo"                   # property (PascalCase name)
    assert got["dto"] == "OrderDto"               # parameter
    assert got["local"] == "Helper"               # var + new
    assert got["other"] == "ICaseProcessor"       # explicitly typed local


def test_generic_type_collapses_to_its_bare_name():
    # A method's container is a plain name, so `List<Order>` has to become `List`.
    assert declared_types(CS, "csharp")["_orders"] == "List"


def test_a_method_is_never_mapped_to_its_return_type():
    """The trap: `IActionResult Create(...)` looks exactly like a typed declaration."""
    got = declared_types(CS, "csharp")
    assert "Create" not in got
    assert "CreateAsync" not in got


def test_conflicting_declarations_are_dropped():
    """Per-file scope means two methods can disagree. Unknown beats wrong."""
    src = """\
class A {
    void One() { Foo x = null; }
    void Two() { Bar x = null; }
    void Three() { Baz y = null; }
}
"""
    got = declared_types(src, "csharp")
    assert "x" not in got, "two types for one name resolves to neither"
    assert got["y"] == "Baz"


def test_vbnet_as_clauses_and_new():
    src = """\
Public Class H
    Private _log As ILogger
    Public Sub Go(ByVal c As Customer)
        Dim h = New Helper()
    End Sub
End Class
"""
    got = declared_types(src, "vbnet")
    assert got["_log"] == "ILogger"
    assert got["c"] == "Customer"
    assert got["h"] == "Helper"


def test_typescript_annotations_and_new():
    src = "class Card { private svc: OrderService; go(a: Foo) { const h = new Helper(); } }"
    got = declared_types(src, "typescript")
    assert got["svc"] == "OrderService"
    assert got["a"] == "Foo"
    assert got["h"] == "Helper"


def test_python_declares_nothing_here():
    # No annotations pass yet; claiming a type from `x = Foo()` is inference, not reading.
    assert declared_types("x = Foo()\n", "python") == {}


# --- base lists -------------------------------------------------------------

@pytest.mark.parametrize("signature,expected", [
    ("class Svc : BaseSvc, ICaseProcessor", ["BaseSvc", "ICaseProcessor"]),
    ("class R : Repo<Order>, IRepo<Order> where T : new()", ["Repo", "IRepo"]),
    ("class Plain", []),
    ("interface IFoo : IDisposable", ["IDisposable"]),
])
def test_base_types_from_csharp_signature(signature, expected):
    assert base_types(signature, "csharp") == expected


def test_vbnet_base_list_is_not_read_from_the_declaration_line():
    # VB uses separate Inherits/Implements statements, which are not in the signature.
    assert base_types("Class LegacyHelper", "vbnet") == []


# --- satisfies --------------------------------------------------------------

def test_satisfies_same_type():
    assert _satisfies("NoteStore", "NoteStore", {}) is True
    assert _satisfies("NoteStore", "notestore", {}) is True


def test_satisfies_walks_one_implements_step():
    impls = {"icaseprocessor": {"CaseProcessor"}}
    assert _satisfies("ICaseProcessor", "CaseProcessor", impls) is True
    assert _satisfies("ICaseProcessor", "Unrelated", impls) is False


def test_satisfies_is_false_when_either_side_is_unknown():
    assert _satisfies("", "CaseProcessor", {}) is False
    assert _satisfies("ICaseProcessor", "", {}) is False


# --- end to end -------------------------------------------------------------

IFACE = {
    "ICaseProcessor.cs": """\
namespace N
{
    public interface ICaseProcessor
    {
        void HandleException(string m);
    }
}
""",
    "CaseProcessor.cs": """\
namespace N
{
    public class CaseProcessor : ICaseProcessor
    {
        public void HandleException(string m) { }
    }
}
""",
    "Controller.cs": """\
namespace N
{
    public class Ctl
    {
        private readonly ICaseProcessor _processor;
        public void Post(string m)
        {
            _processor.HandleException(m);
        }
    }
}
""",
}


@pytest.fixture
def iface_repo(repo):
    src = repo / "src"
    src.mkdir()
    exts = load_extensions(find_cache_root())
    c = Cache(find_cache_root())
    with c.batch():
        for name, body in IFACE.items():
            p = src / name
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    return gi.build(c.list_entries())


def test_implementors_map_is_built_from_base_lists(iface_repo):
    assert iface_repo.implementors.get("icaseprocessor") == {"CaseProcessor"}


def test_an_interface_typed_receiver_resolves_to_the_implementation(iface_repo):
    """The headline case, and why one implements step is worth walking.

    `_processor` is declared `ICaseProcessor`, so the interface's own declaration matches
    it exactly — but that declaration has no body. The class that implements it is the
    answer someone tracing this flow wants.
    """
    graph = iface_repo
    cs = next(c for c in graph.call_sites
              if c["callee"] == "HandleException" and c["caller"] == "Post")
    assert cs["recv_type"] == "ICaseProcessor"
    assert cs["recv_kind"] == RECV_IDENT

    got = graph._best_def("handleexception", cs, "Ctl")
    assert got["container"] == "CaseProcessor", "the implementation, not the interface"
    assert got["kind"] == "method"


def test_both_the_interface_and_the_implementation_stay_in_callers(iface_repo):
    """Resolution picks one target; it must not delete the other edge."""
    rows = iface_repo.callers(gi._collapse("HandleException"))
    assert any(r["caller"] == "Post" for r in rows)
    assert all("tier" in r for r in rows)


def test_a_type_mismatch_is_not_a_contradiction(iface_repo):
    """A method may be declared on a base class, and only one level is tracked.

    Treating an unmatched declared type as proof of absence would drop real edges, so it
    only ever fails to promote.
    """
    graph = iface_repo
    cs = {"caller_path": "x", "caller_line": 0, "recv": "svc",
          "recv_kind": RECV_IDENT, "argc": 1, "recv_type": "SomethingElse"}
    sym = {"container": "CaseProcessor", "path": "y", "pmin": 1, "pmax": 1, "pflags": 0}
    assert _resolve_tier(cs, sym, "Ctl", graph.type_names,
                         graph.implementors) >= 0, "must not be TIER_CONTRA"


def test_declared_type_beats_a_coincidental_name_match(iface_repo):
    """Declared type is real information; a variable named like a type is not."""
    graph = iface_repo
    cs = {"caller_path": "x", "caller_line": 0, "recv": "CaseProcessor",
          "recv_kind": RECV_IDENT, "argc": 1, "recv_type": "ICaseProcessor"}
    sym = {"container": "CaseProcessor", "path": "y", "pmin": 1, "pmax": 1, "pflags": 0}
    assert _resolve_tier(cs, sym, "Ctl", graph.type_names,
                         graph.implementors) == TIER_EXACT
