"""`memo register` / `memo link` / `memo trace --cross-service`.

The registry lives outside any one repo (`~/.memo/registry.json`), so these tests
redirect it into a throwaway location rather than touching the real machine-wide file.
"""

from __future__ import annotations

import pytest

from memo import graphindex as gi
from memo import services
from memo.cache import Cache
from memo.cli import _build_entry
from memo.config import find_cache_root, language_for, load_extensions

ORDERS_CONTROLLER = """\
using System;
namespace Shop {
  public class OrdersController {
    public void Create(int a) { SaveOrder(a); }
    public void SaveOrder(int a) { }
  }
}
"""

ORDERS_SERVICE = """\
using System;
namespace Orders {
  public class OrderStore {
    public void Persist(int a) { }
  }
}
"""


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Redirect the global registry into tmp_path so tests never touch the real one."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(services, "REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr(services, "REGISTRY_FILE", fake_home / ".memo" / "registry.json")
    yield


def build_repo(tmp_path, name, files):
    d = tmp_path / name
    d.mkdir()
    root = d / ".memo"
    exts = load_extensions(root)
    c = Cache(root)
    with c.batch():
        for rel, body in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            c.store(_build_entry(p, language_for(p, exts)))
    g = gi.build(c.list_entries(), cache_root=root)
    gi.save(g, root)
    return d, c, g


# --- registry --------------------------------------------------------------- #

def test_register_and_list(tmp_path):
    d, _c, _g = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    name = services.register(d, "svc-a")
    assert name == "svc-a"
    assert services.list_repos()["svc-a"] == str(d.resolve())


def test_register_defaults_name_to_directory(tmp_path):
    d, _c, _g = build_repo(tmp_path, "svc-b", {"a.cs": ORDERS_CONTROLLER})
    name = services.register(d)
    assert name == "svc-b"


def test_unregister(tmp_path):
    d, _c, _g = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    services.register(d, "svc-a")
    assert services.unregister("svc-a")
    assert "svc-a" not in services.list_repos()
    assert not services.unregister("svc-a")  # already gone


def test_current_repo_name_auto_registers(tmp_path):
    d, _c, _g = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    assert "svc-a" not in services.list_repos()  # not registered yet ("svc-a" is dir name)
    name = services.current_repo_name(d)
    assert name == d.name
    assert d.name in services.list_repos()


# --- links -------------------------------------------------------------- #

def test_add_and_list_and_remove_link(tmp_path):
    da, _c, _g = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    db, _c2, _g2 = build_repo(tmp_path, "svc-b", {"b.cs": ORDERS_SERVICE})
    services.register(da, "svc-a")
    services.register(db, "svc-b")
    services.add_link("SaveOrder", "svc-a", "Persist", "svc-b")
    links = services.list_links()
    assert len(links) == 1
    assert services.remove_link(0)
    assert services.list_links() == []


# --- cross-service trace -------------------------------------------------- #

def test_cross_trace_follows_a_declared_link_down(tmp_path):
    da, _ca, ga = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    db, _cb, gb = build_repo(tmp_path, "svc-b", {"b.cs": ORDERS_SERVICE})
    services.register(da, "svc-a")
    services.register(db, "svc-b")
    services.add_link("SaveOrder", "svc-a", "Persist", "svc-b")

    result = services.cross_trace("svc-a", ga, "Create", direction="down", depth=3)
    assert not result["errors"]

    def flatten(node):
        yield node
        for ch in node["children"]:
            yield from flatten(ch)

    names_by_repo = {(n["repo"], n["name"]) for n in flatten(result["tree"])}
    assert ("svc-a", "Create") in names_by_repo
    assert ("svc-a", "SaveOrder") in names_by_repo
    assert ("svc-b", "Persist") in names_by_repo, "trace did not cross into the linked repo"


def test_cross_trace_reports_unregistered_repo_as_an_error_not_a_crash(tmp_path):
    da, _ca, ga = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    services.register(da, "svc-a")
    services.add_link("SaveOrder", "svc-a", "Persist", "ghost-service")

    result = services.cross_trace("svc-a", ga, "Create", direction="down", depth=3)
    assert result["errors"]
    assert "ghost-service" in result["errors"][0]


def test_render_cross_trace_marks_the_hop(tmp_path):
    da, _ca, ga = build_repo(tmp_path, "svc-a", {"a.cs": ORDERS_CONTROLLER})
    db, _cb, gb = build_repo(tmp_path, "svc-b", {"b.cs": ORDERS_SERVICE})
    services.register(da, "svc-a")
    services.register(db, "svc-b")
    services.add_link("SaveOrder", "svc-a", "Persist", "svc-b")
    result = services.cross_trace("svc-a", ga, "Create", direction="down", depth=3)
    out = services.render_cross_trace(result, terse=True)
    assert "svc-b" in out
