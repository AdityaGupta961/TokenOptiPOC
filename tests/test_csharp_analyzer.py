"""Modern C# constructs the analyzer must not miss.

A symbol memo fails to extract is not merely absent from a summary — it is absent from
the call graph, so `callers`/`impact`/`trace` quietly under-report. That is a worse
failure than being slow, because the rule files tell agents to trust memo's answers.
"""

from __future__ import annotations

import pytest

from memo.summarizer import summarize

SRC = """\
using System;
namespace Shop
{
    public record Money(decimal Amount, string Currency);

    public record class Receipt(int Id);

    public record struct PointR(int X, int Y);

    public interface IRepo<T> where T : class
    {
        Task<T> GetAsync(int id);
    }

    public class Repo<T> : IRepo<T> where T : class
    {
        public event EventHandler? Changed;
        public event EventHandler<int> Progressed;

        public string Name { get; init; } = "default";
        public int Count { get; set; }

        public async Task<T> GetAsync(int id) => await Load<T>(id);

        public async Task<(bool ok, T val)> TryGet<TKey>(TKey key)
        {
            return (true, default!);
        }

        public class Nested
        {
            public void Inner() { }
        }

        private enum Mode { On, Off }
    }
}
"""


@pytest.fixture(scope="module")
def summary():
    return summarize(SRC, "csharp")


@pytest.fixture(scope="module")
def by_name(summary):
    out = {}
    for s in summary.symbols:
        out.setdefault(s.name, []).append(s)
    return out


@pytest.mark.parametrize("name", [
    "Money", "Receipt", "PointR",      # records, incl. `record class` / `record struct`
    "IRepo",                           # generic interface with a where-clause
    "Repo",
    "Changed", "Progressed",           # events
    "Name", "Count",                   # init-only + auto properties
    "GetAsync", "TryGet",              # async, generic method, tuple return
    "Nested", "Inner",                 # nested type and its member
    "Mode",                            # nested enum
])
def test_symbol_is_found(by_name, name):
    assert name in by_name


def test_generic_interface_is_not_swallowed_by_a_preceding_record(by_name):
    """Regression: `record struct X(...);` matched kind=record/name=struct, and its
    unbounded trailing group ran across newlines to the next `{` — consuming the
    following interface declaration so it was never reported."""
    assert "IRepo" in by_name
    assert by_name["IRepo"][0].kind == "interface"


def test_record_variants_all_report_kind_record(by_name):
    for name in ("Money", "Receipt", "PointR"):
        kinds = {s.kind for s in by_name[name]}
        assert "record" in kinds, f"{name} -> {kinds}"


def test_positional_record_has_no_bogus_extent(by_name):
    """It ends in `;` and has no body; brace-matching would grab an unrelated block."""
    rec = next(s for s in by_name["Money"] if s.kind == "record")
    assert rec.end_line in (0, rec.line)


def test_events_are_kind_event_not_property(by_name):
    for name in ("Changed", "Progressed"):
        kinds = {s.kind for s in by_name[name]}
        assert kinds == {"event"}, f"{name} -> {kinds}"


def test_generic_signature_is_preserved(by_name):
    assert "<T>" in next(s for s in by_name["Repo"] if s.kind == "class").signature
    assert "IRepo<T>" in next(s for s in by_name["Repo"] if s.kind == "class").signature


def test_container_extents_span_their_members(by_name):
    repo = next(s for s in by_name["Repo"] if s.kind == "class")
    inner = by_name["Inner"][0]
    nested = next(s for s in by_name["Nested"] if s.kind == "class")
    assert repo.line < nested.line <= inner.line <= nested.end_line <= repo.end_line


def test_every_reported_line_contains_its_symbol(summary):
    lines = SRC.splitlines()
    for s in summary.symbols:
        assert 1 <= s.line <= len(lines), f"{s.name} line {s.line}"
        leaf = s.name.rsplit(".", 1)[-1]
        assert leaf in lines[s.line - 1], \
            f"{s.name} reported at :{s.line} -> {lines[s.line - 1]!r}"


def test_no_inverted_extents(summary):
    for s in summary.symbols:
        if s.end_line:
            assert s.end_line >= s.line, f"{s.name}: {s.line}-{s.end_line}"


def test_expression_bodied_member_has_no_block_extent(by_name):
    """`=> await Load<T>(id);` — a one-liner, not a brace block."""
    g = by_name["GetAsync"][0]
    assert g.end_line in (0, g.line)


def test_dependencies_captured(summary):
    assert "System" in summary.dependencies
