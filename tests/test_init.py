"""`memo init` end to end — the onboarding path a new user's first impression rests on."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from memo.cli import main
from memo.targets import ALL, TARGETS


def run(*args):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


# --- scaffolding ------------------------------------------------------------

def test_writes_every_rule_file(sample_repo):
    run("init", "--no-index")
    for key in ALL:
        dest = sample_repo / TARGETS[key].rel_path
        assert dest.is_file(), f"{TARGETS[key].rel_path} was not written"
        assert dest.read_text(encoding="utf-8").strip()


def test_adds_memo_to_gitignore_and_seeds_memoignore(sample_repo):
    run("init", "--no-index")
    assert ".memo/" in (sample_repo / ".gitignore").read_text(encoding="utf-8")
    assert (sample_repo / ".memoignore").is_file()


def test_writes_claude_and_cursor_hooks_by_default(sample_repo):
    run("init", "--no-index")
    claude_settings = json.loads((sample_repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "SessionStart" in claude_settings["hooks"]
    cursor_hooks = json.loads((sample_repo / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert "sessionStart" in cursor_hooks["hooks"]


def test_no_hooks_flag_skips_hook_files(sample_repo):
    run("init", "--no-index", "--no-hooks")
    assert not (sample_repo / ".claude" / "settings.json").is_file()
    assert not (sample_repo / ".cursor" / "hooks.json").is_file()


def test_hooks_only_written_for_selected_agents(sample_repo):
    run("init", "--no-index", "--agent", "claude")
    assert (sample_repo / ".claude" / "settings.json").is_file()
    assert not (sample_repo / ".cursor" / "hooks.json").is_file()


def test_init_rerun_does_not_duplicate_hook_entries(sample_repo):
    run("init", "--no-index")
    run("init", "--no-index")  # second run: must be idempotent, not accumulate entries
    claude_settings = json.loads((sample_repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert len(claude_settings["hooks"]["SessionStart"]) == 1


def test_gitignore_entry_not_duplicated(sample_repo):
    (sample_repo / ".gitignore").write_text("node_modules/\n.memo/\n", encoding="utf-8")
    run("init", "--no-index")
    text = (sample_repo / ".gitignore").read_text(encoding="utf-8")
    assert text.count(".memo/") == 1
    assert "node_modules/" in text


def test_existing_gitignore_content_preserved(sample_repo):
    (sample_repo / ".gitignore").write_text("bin/\nobj/\n", encoding="utf-8")
    run("init", "--no-index")
    text = (sample_repo / ".gitignore").read_text(encoding="utf-8")
    assert "bin/" in text and "obj/" in text and ".memo/" in text


def test_existing_memoignore_is_not_overwritten(sample_repo):
    (sample_repo / ".memoignore").write_text("# mine\nweird/\n", encoding="utf-8")
    run("init", "--no-index")
    assert (sample_repo / ".memoignore").read_text(encoding="utf-8") == "# mine\nweird/\n"


# --- idempotency, the property that makes init safe to re-run ---------------

def test_rerun_changes_nothing(sample_repo):
    run("init", "--no-index")
    snapshot = {
        p.relative_to(sample_repo).as_posix(): p.read_bytes()
        for p in sample_repo.rglob("*") if p.is_file() and ".memo" not in p.parts
    }
    out = run("init", "--no-index").output
    assert "unchanged" in out
    after = {
        p.relative_to(sample_repo).as_posix(): p.read_bytes()
        for p in sample_repo.rglob("*") if p.is_file() and ".memo" not in p.parts
    }
    assert after == snapshot


def test_preserves_preexisting_agents_md(sample_repo):
    """The common case: the repo already has house rules in the file memo writes to."""
    original = "# House rules\n\nRun `dotnet test` first.\n"
    (sample_repo / "AGENTS.md").write_text(original, encoding="utf-8")
    run("init", "--no-index")
    text = (sample_repo / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(original), "every original byte must survive, in order"
    assert "memo callers" in text


def test_hand_edited_block_is_skipped_then_forced(sample_repo):
    run("init", "--no-index")
    target = sample_repo / TARGETS["copilot"].rel_path
    tampered = target.read_text(encoding="utf-8").replace(
        "| Need | Command |", "| Need | Command | (ours) |")
    target.write_text(tampered, encoding="utf-8")

    out = run("init", "--no-index").output
    assert "hand-edited" in out
    assert target.read_text(encoding="utf-8") == tampered, "must not clobber without --force"

    run("init", "--no-index", "--force")
    assert "(ours)" not in target.read_text(encoding="utf-8")


# --- flags ------------------------------------------------------------------

def test_dry_run_writes_nothing(sample_repo):
    run("init", "--dry-run")
    for key in ALL:
        assert not (sample_repo / TARGETS[key].rel_path).exists()
    assert not (sample_repo / ".memo").exists()


def test_print_writes_nothing_and_emits_all_targets(sample_repo):
    out = run("init", "--print").output
    for key in ALL:
        assert TARGETS[key].rel_path in out
    assert not (sample_repo / "AGENTS.md").exists()


def test_each_target_is_self_contained(sample_repo):
    """No surface imports another any more, so selecting one writes exactly one file."""
    run("init", "--no-index", "--agent", "claude")
    assert (sample_repo / ".claude/skills/memo/SKILL.md").is_file()
    assert not (sample_repo / "AGENTS.md").exists(), "the skill needs no companion file"
    assert not (sample_repo / ".cursor/rules/memo.mdc").exists()


def test_agent_subset_writes_only_what_was_asked_for(sample_repo):
    run("init", "--no-index", "--agent", "cursor")
    assert (sample_repo / ".cursor/rules/memo.mdc").is_file()
    assert not (sample_repo / ".claude/skills/memo/SKILL.md").exists()
    assert not (sample_repo / "AGENTS.md").exists()


def test_the_skill_is_generated_not_hand_maintained(sample_repo):
    """Regression: the skill was hand-written and outside init, so it drifted.

    It sat a version behind the guide — missing a whole section, and still carrying a
    grammar artifact the template had already fixed. Single-sourcing it is the same fix
    that retired the hand-maintained copies in `integrations/`.
    """
    run("init", "--no-index", "--agent", "claude")
    text = (sample_repo / ".claude/skills/memo/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "frontmatter must be on line 1"
    assert "confidence marker" in text, "must carry the current guide, not a stale copy"
    assert "{{" not in text, "no unsubstituted placeholder may ship"


def test_unknown_agent_is_rejected(sample_repo):
    result = CliRunner().invoke(main, ["init", "--no-index", "--agent", "notanide"])
    assert result.exit_code != 0
    assert "Unknown agent" in result.output


# --- indexing ---------------------------------------------------------------

def test_init_builds_a_usable_index(sample_repo):
    run("init")
    assert (sample_repo / ".memo" / "index.json").is_file()
    assert (sample_repo / ".memo" / "graph.json").is_file()
    # The call edge Total -> Add must be discoverable, which is the whole point.
    out = run("callers", "Add", "--json").output
    assert "Total" in out


def test_status_reports_current_after_init(sample_repo):
    run("init")
    report = json.loads(run("status", "--json").output)
    assert report["indexed_files"] >= 1
    assert report["stale_files"] == 0
    assert report["deleted_files"] == 0
    assert report["graph_matches_cache"] is True
    assert set(report["rule_files"].values()) == {"current"}


def test_status_detects_modified_and_deleted_files(sample_repo):
    run("init")
    (sample_repo / "src" / "Orders.cs").write_text("// gutted\n", encoding="utf-8")
    report = json.loads(run("status", "--json").output)
    assert report["stale_files"] == 1

    (sample_repo / "src" / "Orders.cs").unlink()
    report = json.loads(run("status", "--json").output)
    assert report["deleted_files"] == 1


def test_status_flags_entries_built_by_an_older_memo(sample_repo):
    """Content hashing can't see analyzer changes, so an unchanged file indexed by an
    older memo still looks fresh while its line numbers may be wrong. Must be visible."""
    run("init")
    assert json.loads(run("status", "--json").output)["outdated_analyzer_files"] == 0

    from memo.cache import Cache
    from memo.config import find_cache_root

    # Simulate a memo upgrade: the files and the index are untouched, only the version
    # that produced the cached entries is older. Both the per-file entry and the
    # manifest must reflect that, since that is the state an upgrade actually leaves.
    c = Cache(find_cache_root())
    entries = c.list_entries()
    entries[0].memo_version = "0.0.1-ancient"
    import json as _j
    (c.cache_dir / f"{entries[0].content_hash}.json").write_text(
        _j.dumps(entries[0].to_json()), encoding="utf-8")
    c.write_manifest(entries)

    report = json.loads(run("status", "--json").output)
    assert report["outdated_analyzer_files"] == 1
    assert report["stale_files"] == 0, "the file itself did not change"


def test_status_json_is_clean_stdout(sample_repo):
    """Diagnostics must not leak into stdout or agents parsing --json will break."""
    run("init")
    json.loads(run("status", "--json").output)  # raises if anything else was printed
