"""Analyzer behaviour, locked in against the repo's own sample files.

These are characterization tests: they pin down what each analyzer currently extracts
so the planned end_line work (and any regex tightening) can't silently regress symbol
discovery. Assertions target facts a developer would care about — that the class, its
methods, and their declaration lines are found — not exact counts, which are brittle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memo.summarizer import summarize

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def analyze(filename: str, language: str):
    text = (SAMPLES / filename).read_text(encoding="utf-8", errors="replace")
    return summarize(text, language)


def names(summary) -> set[str]:
    return {s.name for s in summary.symbols}


def short(summary) -> set[str]:
    return {s.name.rsplit(".", 1)[-1] for s in summary.symbols}


@pytest.mark.parametrize("filename,language", [
    ("OrdersController.cs", "csharp"),
    ("LegacyHelper.vb", "vbnet"),
    ("UserCard.tsx", "typescript"),
    ("Default.aspx", "markup"),
])
def test_sample_yields_symbols_with_sane_lines(filename, language):
    s = analyze(filename, language)
    assert s.symbols, f"{filename}: no symbols extracted"
    for sym in s.symbols:
        assert sym.name, "every symbol must be named"
        assert sym.kind, "every symbol must have a kind"
        # graphindex drops symbols without a truthy line, so a 0/None here means the
        # symbol silently vanishes from the call graph.
        assert isinstance(sym.line, int) and sym.line > 0, f"{sym.name} has line {sym.line!r}"


@pytest.mark.parametrize("filename,language", [
    ("OrdersController.cs", "csharp"),
    ("LegacyHelper.vb", "vbnet"),
    ("UserCard.tsx", "typescript"),
])
def test_declaration_lines_point_at_the_symbol(filename, language):
    """A wrong line number is worse than no line: memo tells agents to trust them."""
    lines = (SAMPLES / filename).read_text(encoding="utf-8", errors="replace").splitlines()
    s = analyze(filename, language)
    for sym in s.symbols:
        assert 1 <= sym.line <= len(lines)
        declaration = lines[sym.line - 1]
        leaf = sym.name.rsplit(".", 1)[-1]
        assert leaf in declaration, (
            f"{filename}:{sym.line} does not contain {leaf!r} -> {declaration!r}")


def test_csharp_finds_class_and_methods():
    s = analyze("OrdersController.cs", "csharp")
    kinds = {sym.kind for sym in s.symbols}
    assert "class" in kinds
    assert kinds & {"method", "constructor"}, "no methods found in a controller"
    assert s.dependencies, "using directives should surface as dependencies"


def test_vbnet_finds_subs_or_functions():
    s = analyze("LegacyHelper.vb", "vbnet")
    assert {sym.kind for sym in s.symbols} & {"method", "function", "class", "module"}


def test_tsx_detects_a_react_component():
    s = analyze("UserCard.tsx", "typescript")
    assert "component" in {sym.kind for sym in s.symbols}, \
        "a .tsx returning JSX should be recognized as a component"


def test_python_analyzer_uses_ast_precisely():
    src = (
        "import os\n"
        "\n"
        "def top_level(a, b=1):\n"
        '    """Does a thing."""\n'
        "    return a + b\n"
        "\n"
        "class Widget:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "    async def async_method(self):\n"
        "        return 2\n"
    )
    s = summarize(src, "python")
    assert "top_level" in names(s)
    assert {"Widget.method", "Widget.async_method"} <= names(s)
    by_name = {sym.name: sym for sym in s.symbols}
    assert by_name["top_level"].line == 3
    assert by_name["Widget.method"].line == 8
    assert by_name["top_level"].doc == "Does a thing."
    assert "os" in s.dependencies


def test_python_syntax_error_degrades_without_raising():
    """A file mid-edit must not break a whole cache-dir run."""
    s = summarize("def broken(:\n    pass\n", "python")
    assert s is not None


@pytest.mark.parametrize("language", ["csharp", "vbnet", "typescript", "javascript",
                                      "markup", "python", "generic"])
def test_empty_input_is_safe_for_every_language(language):
    s = summarize("", language)
    assert s is not None
    assert s.symbols == []
