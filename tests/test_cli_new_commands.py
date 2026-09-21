"""CLI-level smoke tests for every command added on top of the original surface:
`query`, `schema`, `ingest-traces`, `adr *`, `register`/`unregister`/`registry`,
`link`/`unlink`/`suggest-links`, `neighbors`, `peek --at`.

These exist because the library layer (tests/test_query.py, test_adr.py, etc.) tests
the functions directly and cannot catch a *wiring* bug — a CLI command whose option
name doesn't match what the function expects, or a subcommand that was written but
never attached to its group. `adr status` shipped with exactly that bug: the library
function existed, nothing called it. Every command here is invoked through the real
`click` entry point for that reason.
"""

from __future__ import annotations

import json
import sys

from click.testing import CliRunner

from memo.cli import main

PROD_CS = """\
using System;
namespace Shop {
  public class OrderService {
    public int Total(int a, int b) { return Add(a, b); }
    public int Add(int a, int b) { return a + b; }
    public int Lonely(int a) { return a; }
  }
}
"""


def run(*args, expect_ok=True):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    if expect_ok:
        assert result.exit_code == 0, result.output
    return result


def indexed(repo):
    (repo / "src").mkdir()
    (repo / "src" / "OrderService.cs").write_text(PROD_CS, encoding="utf-8")
    run("cache-dir", str(repo), "-r", "-q")
    return repo


# --- query / schema ---------------------------------------------------------- #

def test_query_symbols(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("query", "symbols kind=method", "--terse")
    assert "Add" in r.output


def test_query_symbols_pagerank_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("query", "symbols pagerank>=0", "--terse")
    assert "pagerank=" in r.output

    r = run("query", "symbols pagerank>=0", "--json")
    rows = json.loads(r.output)["rows"]
    assert rows and all("pagerank" in row for row in rows)


def test_brief_wires_through_graph_pagerank(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("brief", "add", "--terse")
    assert r.exit_code == 0


def test_query_bad_expression_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("query", "symbols bogus=1", expect_ok=False)
    assert r.exit_code != 0
    assert "bogus" in r.output


def test_query_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("query", "symbols kind=method", "--count")
    assert r.output.strip().isdigit()


def test_schema_runs(sample_repo):
    r = run("schema")
    assert "confidence tiers" in r.output.lower()


def test_schema_json(sample_repo):
    r = run("schema", "--json")
    data = json.loads(r.output)
    assert "verified" in data["confidence_tiers"].values()


# --- ingest-traces ------------------------------------------------------------ #

def test_ingest_traces_from_stdin_reports_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = CliRunner().invoke(main, ["ingest-traces"],
                           input='{"caller": "Total", "callee": "Lonely"}\n',
                           catch_exceptions=False)
    assert r.exit_code == 0
    assert "resolved" in r.output


def test_ingest_traces_unresolved_callee_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = CliRunner().invoke(main, ["ingest-traces"],
                           input='{"caller": "Total", "callee": "NoSuchThing"}\n',
                           catch_exceptions=False)
    assert r.exit_code == 0
    assert "did not match" in r.output


def test_ingest_traces_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    CliRunner().invoke(main, ["ingest-traces"],
                       input='{"caller": "Total", "callee": "Lonely"}\n')
    r = run("ingest-traces", "--clear")
    assert "Cleared" in r.output


def test_ingest_traces_no_input_is_a_clean_error(sample_repo):
    r = CliRunner().invoke(main, ["ingest-traces"], input="", catch_exceptions=False)
    assert r.exit_code != 0


# --- adr ----------------------------------------------------------------- #

def test_adr_add_list_show_status_delete_roundtrip(sample_repo):
    run("adr", "add", "Use flat JSON", "--affects", "Cache,Graph")
    listing = run("adr", "list")
    assert "Use flat JSON" in listing.output

    shown = run("adr", "show", "1")
    assert "Use flat JSON" in shown.output

    # This is the exact command that was missing entirely in the previous turn.
    status = run("adr", "status", "1", "superseded")
    assert "superseded" in status.output
    assert run("adr", "show", "1", "--json").output.__contains__("superseded")

    deleted = run("adr", "delete", "1")
    assert "Deleted" in deleted.output
    run("adr", "show", "1", expect_ok=False)


def test_adr_status_unknown_id_errors(sample_repo):
    r = run("adr", "status", "999", "accepted", expect_ok=False)
    assert r.exit_code != 0


def test_adr_delete_unknown_id_errors(sample_repo):
    r = run("adr", "delete", "999", expect_ok=False)
    assert r.exit_code != 0


def test_adr_for_symbol(sample_repo):
    run("adr", "add", "Cache design", "--affects", "Cache")
    r = run("adr", "for", "Cache", "--terse")
    assert "ADR-1" in r.output


def test_adr_add_json_echoes_created_record(sample_repo):
    r = run("adr", "add", "Decision A", "--json")
    data = json.loads(r.output)
    assert data["title"] == "Decision A"


# --- map surfaces a governing ADR ------------------------------------------ #

def test_map_shows_governing_adr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    run("adr", "add", "Order totals design", "--affects", "Total")
    r = run("map", "Total")
    assert "ADR-1" in r.output


# --- neighbors / peek --at ------------------------------------------------- #

def test_neighbors_combines_callers_and_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("neighbors", "Total", "--terse")
    assert "callers" in r.output
    assert "calls" in r.output


def test_peek_at_coordinate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("peek", "--at", "OrderService.cs:4")
    assert "Total" in r.output


def test_peek_at_bad_format_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    indexed(tmp_path)
    r = run("peek", "--at", "not-a-coordinate")
    assert "expected" in r.output.lower()


def test_peek_without_symbol_or_at_is_a_clean_error(sample_repo):
    r = run("peek", expect_ok=False)
    assert r.exit_code != 0


# --- register / unregister / registry / link / unlink ---------------------- #

def test_registry_roundtrip(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")

    repo_a = tmp_path / "svc-a"
    repo_a.mkdir()
    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("MEMO_HOME", str(repo_a))

    r = run("register", str(repo_a), "--name", "svc-a", "--json")
    assert json.loads(r.output)["name"] == "svc-a"

    reg = run("registry")
    assert "svc-a" in reg.output

    r = run("unregister", "svc-a")
    assert "Unregistered" in r.output

    r = run("unregister", "svc-a")  # already gone
    assert "was not registered" in r.output


def test_link_requires_registered_target(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")
    repo = tmp_path / "svc-a"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MEMO_HOME", str(repo))

    r = run("link", "Foo", "ghost-service", "Bar", expect_ok=False)
    assert r.exit_code != 0
    assert "not registered" in r.output


def test_link_and_unlink(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")

    repo_a, repo_b = tmp_path / "svc-a", tmp_path / "svc-b"
    repo_a.mkdir(); repo_b.mkdir()
    run("register", str(repo_a), "--name", "svc-a")
    run("register", str(repo_b), "--name", "svc-b")

    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("MEMO_HOME", str(repo_a))
    r = run("link", "Foo", "svc-b", "Bar", "--from-repo", "svc-a", "--json")
    assert json.loads(r.output)["to"] == "Bar"

    reg = run("registry", "--json")
    assert len(json.loads(reg.output)["links"]) == 1

    run("unlink", "0")
    reg2 = run("registry", "--json")
    assert json.loads(reg2.output)["links"] == []


def test_unregister_warns_about_dangling_links(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")

    repo_a, repo_b = tmp_path / "svc-a", tmp_path / "svc-b"
    repo_a.mkdir(); repo_b.mkdir()
    run("register", str(repo_a), "--name", "svc-a")
    run("register", str(repo_b), "--name", "svc-b")
    run("link", "Foo", "svc-b", "Bar", "--from-repo", "svc-a")

    r = run("unregister", "svc-b")
    assert "link" in r.output.lower()
    assert "dangling" in r.output.lower() or "reference an unregistered repo" in r.output


def test_trace_cross_service(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")

    repo_a, repo_b = tmp_path / "svc-a", tmp_path / "svc-b"
    repo_a.mkdir(); repo_b.mkdir()
    (repo_a / "a.cs").write_text(
        "using System;\n"
        "namespace A {\n"
        "  public class C {\n"
        "    public void Create() { SaveOrder(); }\n"
        "    public void SaveOrder() { }\n"
        "  }\n"
        "}\n",
        encoding="utf-8")
    (repo_b / "b.cs").write_text(
        "using System;\n"
        "namespace B {\n"
        "  public class Store {\n"
        "    public void Persist() { }\n"
        "  }\n"
        "}\n",
        encoding="utf-8")

    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("MEMO_HOME", str(repo_a))
    run("cache-dir", str(repo_a), "-r", "-q")
    run("register", str(repo_a), "--name", "svc-a")

    monkeypatch.chdir(repo_b)
    monkeypatch.setenv("MEMO_HOME", str(repo_b))
    run("cache-dir", str(repo_b), "-r", "-q")
    run("register", str(repo_b), "--name", "svc-b")

    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("MEMO_HOME", str(repo_a))
    run("link", "SaveOrder", "svc-b", "Persist", "--from-repo", "svc-a")

    r = run("trace", "Create", "--down", "--cross-service", "--terse")
    assert "svc-b" in r.output
    assert "Persist" in r.output


# --- run / recall -------------------------------------------------------------- #

def test_run_wires_through_to_stdout_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    r = CliRunner().invoke(
        main, ["run", "--", sys.executable, "-c", "print('hello from run')"],
        catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "hello from run" in r.output


def test_run_nonzero_exit_code_propagates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    r = CliRunner().invoke(
        main, ["run", "--", sys.executable, "-c", "import sys; sys.exit(5)"],
        catch_exceptions=False)
    assert r.exit_code == 5


def test_run_with_no_command_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    r = CliRunner().invoke(main, ["run"], catch_exceptions=False)
    assert r.exit_code != 0
    assert "no command given" in r.output


def test_run_then_recall_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    script = "print('\\n'.join(f'noisy line {i}' for i in range(500)))"
    r = CliRunner().invoke(main, ["run", "--", sys.executable, "-c", script],
                            catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "memo recall" in r.output

    import re
    m = re.search(r"memo recall (\w+)", r.output)
    assert m, r.output
    h = m.group(1)

    r2 = CliRunner().invoke(main, ["recall", h], catch_exceptions=False)
    assert r2.exit_code == 0, r2.output
    assert "noisy line 0" in r2.output
    assert "noisy line 499" in r2.output


def test_recall_unknown_hash_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    r = CliRunner().invoke(main, ["recall", "a" * 64], catch_exceptions=False)
    assert r.exit_code != 0
    assert "no stored output" in r.output


# --- shim ------------------------------------------------------------------- #

def _isolate_shim_dir(monkeypatch, tmp_path):
    shim_dir = tmp_path / "home" / ".memo" / "shims"
    monkeypatch.setattr("memo.shim.SHIM_DIR", shim_dir)
    monkeypatch.setattr("memo.shim.REGISTRY_FILE", shim_dir / "registry.json")
    return shim_dir


def test_shim_install_list_remove_roundtrip(tmp_path, monkeypatch):
    shim_dir = _isolate_shim_dir(monkeypatch, tmp_path)
    tool_dir = tmp_path / "toolbin"
    tool_dir.mkdir()
    tool = tool_dir / "mytool"
    tool.write_text("#!/usr/bin/env sh\necho hi\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir))

    r = run("shim", "install", "mytool")
    assert "Shimmed 'mytool'" in r.output
    assert (shim_dir / "mytool").is_file()

    r = run("shim", "list")
    assert "mytool" in r.output

    r = run("shim", "list", "--json")
    assert json.loads(r.output)["mytool"]["realpath"] == str(tool)

    r = run("shim", "remove", "mytool")
    assert "Removed shim" in r.output
    assert not (shim_dir / "mytool").exists()


def test_shim_install_refuses_memo(tmp_path, monkeypatch):
    _isolate_shim_dir(monkeypatch, tmp_path)
    r = run("shim", "install", "memo", expect_ok=False)
    assert r.exit_code != 0
    assert "recurse" in r.output


def test_shim_list_empty_message(tmp_path, monkeypatch):
    _isolate_shim_dir(monkeypatch, tmp_path)
    r = run("shim", "list")
    assert "No shims installed" in r.output


def test_shim_remove_nonexistent_is_not_an_error(tmp_path, monkeypatch):
    _isolate_shim_dir(monkeypatch, tmp_path)
    r = run("shim", "remove", "never-installed")
    assert "No shim installed" in r.output


def test_doctor_reports_shadowed_shim(tmp_path, monkeypatch):
    shim_dir = _isolate_shim_dir(monkeypatch, tmp_path)
    tool_dir = tmp_path / "toolbin"
    tool_dir.mkdir()
    tool = tool_dir / "mytool"
    tool.write_text("#!/usr/bin/env sh\necho hi\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir))
    run("shim", "install", "mytool")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMO_HOME", str(tmp_path))
    # Real tool's dir still ahead of the shim dir on PATH -> shadowed.
    r = run("doctor")
    assert "shim 'mytool' on PATH" in r.output
    assert "shadowed" in r.output.lower() or "memo doctor" in r.output.lower()


def test_suggest_links_runs_without_crashing(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("memo.services.REGISTRY_DIR", fake_home / ".memo")
    monkeypatch.setattr("memo.services.REGISTRY_FILE", fake_home / ".memo" / "registry.json")
    repo = tmp_path / "svc-a"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MEMO_HOME", str(repo))
    indexed(repo)
    r = run("suggest-links")
    assert "Suggested cross-service links" in r.output
