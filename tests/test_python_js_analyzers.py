"""Python and JS/TS constructs the analyzers must not miss.

Both files previously dropped whole categories of symbol: Python lost anything inside a
nested class, and JS/TS misclassified implicit-return arrow components. A dropped symbol
is a missing call-graph node, so `callers`/`impact` silently under-report.
"""

from __future__ import annotations

import pytest

from memo.summarizer import summarize

PY_SRC = '''\
import functools
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    name: str

    @property
    def upper(self) -> str:
        return self.name.upper()

    @staticmethod
    def make() -> "Config":
        return Config("x")

    @classmethod
    def default(cls):
        return cls("d")

    class Inner:
        def deep(self):
            return 1

        class Deeper:
            def deepest(self):
                return 2

@functools.lru_cache(maxsize=8)
def cached_helper(a, b=2):
    return a + b

async def fetch(url):
    return url
'''

TS_SRC = """\
import React, { useState } from "react";

export const Badge = () => <span className="badge" />;

export const Spread = () => {
  const a = 1; const b = 2; const c = 3; const d = 4; const e = 5;
  const f = 1; const g = 2; const h = 3; const i = 4; const j = 5;
  const k = 1; const l = 2; const m = 3; const n = 4; const o = 5;
  const p = 1; const q = 2; const r = 3; const s = 4; const t = 5;
  const u = 1; const v = 2; const w = 3; const x = 4; const y = 5;
  return <div>{a}</div>;
};

export function Card({ title }) {
  return <div>{title}</div>;
}

export const Parse = (s: string) => JSON.parse(s) as Array<string>;
export const Pick = <T, K extends keyof T>(o: T, k: K) => o[k];

export class Store {
  static create() { return new Store(); }
  async load() { return 1; }
}
"""


# --- Python -----------------------------------------------------------------

@pytest.fixture(scope="module")
def py():
    return {s.name: s for s in summarize(PY_SRC, "python").symbols}


@pytest.mark.parametrize("name", [
    "Config", "Config.upper", "Config.make", "Config.default",
    "Config.Inner", "Config.Inner.deep",
    "Config.Inner.Deeper", "Config.Inner.Deeper.deepest",
    "cached_helper", "fetch",
])
def test_python_symbol_found(py, name):
    assert name in py


def test_nested_classes_are_recursive_not_two_levels(py):
    """Regression: only top-level classes and their direct methods were walked, so
    anything inside a nested class vanished from the summary and the call graph."""
    assert "Config.Inner.Deeper.deepest" in py


def test_property_decorator_changes_kind(py):
    """A @property is read as an attribute, so `name(` never appears for it — treating
    it as a method mis-ranks it against real logic in peek/brief."""
    assert py["Config.upper"].kind == "property"
    assert py["Config.make"].kind == "method"
    assert py["Config.default"].kind == "method"


@pytest.mark.parametrize("name,decorator", [
    ("Config", "@dataclass"),
    ("Config.upper", "@property"),
    ("Config.make", "@staticmethod"),
    ("Config.default", "@classmethod"),
    ("cached_helper", "@functools.lru_cache"),
])
def test_decorators_surface_in_signature(py, name, decorator):
    assert decorator in py[name].signature


def test_async_is_visible(py):
    assert "async def" in py["fetch"].signature


def test_python_extents_are_exact_and_nested(py):
    outer, inner = py["Config"], py["Config.Inner"]
    assert outer.line < inner.line
    assert inner.end_line <= outer.end_line
    assert py["Config.Inner.Deeper"].end_line <= inner.end_line


def test_python_lines_point_at_declarations(py):
    lines = PY_SRC.splitlines()
    for s in py.values():
        leaf = s.name.rsplit(".", 1)[-1]
        assert leaf in lines[s.line - 1], f"{s.name} at :{s.line}"


# --- JS / TS ----------------------------------------------------------------

@pytest.fixture(scope="module")
def ts():
    return {s.name: s for s in summarize(TS_SRC, "typescript").symbols}


@pytest.mark.parametrize("name", [
    "Badge", "Spread", "Card", "Parse", "Pick", "Store",
    "Store.create", "Store.load",
])
def test_ts_symbol_found(ts, name):
    assert name in ts


def test_implicit_return_arrow_is_a_component(ts):
    """`() => <span/>` has no `return` keyword — one of the commonest component forms,
    previously reported as a plain function."""
    assert ts["Badge"].kind == "component"


def test_component_detected_beyond_the_old_400_char_window(ts):
    assert ts["Spread"].kind == "component"


def test_function_declaration_component(ts):
    assert ts["Card"].kind == "component"


def test_generic_type_argument_is_not_mistaken_for_jsx(ts):
    """`as Array<string>` must not make this look like a component."""
    assert ts["Parse"].kind == "function"


def test_generic_arrow_function_is_found(ts):
    """`<T, K extends keyof T>(o, k) => ...` was previously invisible."""
    assert ts["Pick"].kind == "function"


def test_ts_lines_point_at_declarations(ts):
    lines = TS_SRC.splitlines()
    for s in ts.values():
        leaf = s.name.replace("#", ".").rsplit(".", 1)[-1]
        assert leaf in lines[s.line - 1], f"{s.name} at :{s.line}"


def test_ts_no_inverted_extents(ts):
    for s in ts.values():
        if s.end_line:
            assert s.end_line >= s.line, f"{s.name}: {s.line}-{s.end_line}"
