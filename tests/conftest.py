"""Shared fixtures.

`memo init` pins MEMO_HOME so downstream calls agree on the repo root. That is right
for a CLI process but leaks between tests in-process, so it is saved and restored
around every test.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clean_memo_env(monkeypatch):
    monkeypatch.delenv("MEMO_HOME", raising=False)
    yield


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty throwaway repo, with CWD pointed at it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def sample_repo(repo):
    """A repo with one indexable C# file containing a real caller->callee edge."""
    src = repo / "src"
    src.mkdir()
    (src / "Orders.cs").write_text(
        "using System;\n"
        "namespace Shop {\n"
        "  public class OrderService {\n"
        "    public int Total(int a, int b) { return Add(a, b); }\n"
        "    public int Add(int a, int b) { return a + b; }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return repo
