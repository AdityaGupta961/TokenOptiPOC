"""`memo.hooks` — guaranteed-delivery Claude Code/Cursor hooks written by `memo init`.

Covers both the JSON-merge logic (idempotent, non-destructive to unrelated hooks)
and, for the shell commands themselves, actual subprocess execution — a hook that
merges correctly but has a shell-escaping bug would be worse than useless, so these
don't stop at asserting string shape.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from memo import hooks


# --- Claude Code: merge logic ------------------------------------------------

def test_build_claude_settings_from_empty():
    settings = hooks.build_claude_settings({})
    session_start = settings["hooks"]["SessionStart"]
    assert len(session_start) == 1
    assert "hooks" in session_start[0] and "matcher" not in session_start[0]

    pre = settings["hooks"]["PreToolUse"]
    matchers = {e["matcher"] for e in pre}
    assert matchers == {"Grep", "Bash"}


def test_build_claude_settings_preserves_unrelated_top_level_keys():
    existing = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {}}
    settings = hooks.build_claude_settings(existing)
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}


def test_build_claude_settings_preserves_unrelated_hook_events():
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]}}
    settings = hooks.build_claude_settings(existing)
    assert settings["hooks"]["Stop"] == existing["hooks"]["Stop"]


def test_build_claude_settings_preserves_users_own_grep_hook():
    """A user's own PreToolUse/Grep hook (not memo's) must survive alongside memo's,
    not be silently deleted just because memo also targets that matcher."""
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Grep", "hooks": [{"type": "command", "command": "my-own-audit-script"}]},
            ]
        }
    }
    settings = hooks.build_claude_settings(existing)
    grep_entries = [e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == "Grep"]
    commands = [h["command"] for e in grep_entries for h in e["hooks"]]
    assert "my-own-audit-script" in commands
    assert any(hooks._is_memo_command(c) for c in commands)


def test_build_claude_settings_is_idempotent():
    once = hooks.build_claude_settings({})
    twice = hooks.build_claude_settings(once)
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_build_claude_settings_replaces_stale_memo_entry_not_duplicates():
    once = hooks.build_claude_settings({})
    # Simulate a hand-edited/older memo entry with a different command under the
    # same marker — re-running init must replace it, not accumulate a second one.
    once["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
        f": {hooks._MARKER}\nprintf 'stale text'"
    )
    twice = hooks.build_claude_settings(once)
    assert len(twice["hooks"]["SessionStart"]) == 1
    assert "stale text" not in twice["hooks"]["SessionStart"][0]["hooks"][0]["command"]


# --- Claude Code: file I/O ----------------------------------------------------

def test_write_claude_code_hooks_created_then_unchanged(tmp_path):
    status1 = hooks.write_claude_code_hooks(tmp_path)
    assert status1 == "created"
    dest = tmp_path / ".claude" / "settings.json"
    assert dest.is_file()

    status2 = hooks.write_claude_code_hooks(tmp_path)
    assert status2 == "unchanged"


def test_write_claude_code_hooks_dry_run_writes_nothing(tmp_path):
    status = hooks.write_claude_code_hooks(tmp_path, dry_run=True)
    assert status == "created"
    assert not (tmp_path / ".claude" / "settings.json").is_file()


def test_write_claude_code_hooks_preserves_existing_file_on_conflict(tmp_path):
    dest = tmp_path / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{not valid json", encoding="utf-8")
    status = hooks.write_claude_code_hooks(tmp_path)
    assert status == "conflict"
    assert dest.read_text(encoding="utf-8") == "{not valid json"


def test_write_claude_code_hooks_preserves_existing_permissions(tmp_path):
    dest = tmp_path / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"permissions": {"allow": ["Bash(git *)"]}}), encoding="utf-8")
    hooks.write_claude_code_hooks(tmp_path)
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(git *)"]}
    assert "SessionStart" in data["hooks"]


# --- Cursor --------------------------------------------------------------------

def test_build_cursor_hooks_from_empty():
    config = hooks.build_cursor_hooks({})
    assert config["version"] == 1
    assert len(config["hooks"]["sessionStart"]) == 1


def test_build_cursor_hooks_preserves_other_events():
    existing = {"hooks": {"afterFileEdit": [{"command": "./lint.sh"}]}}
    config = hooks.build_cursor_hooks(existing)
    assert config["hooks"]["afterFileEdit"] == [{"command": "./lint.sh"}]


def test_build_cursor_hooks_is_idempotent():
    once = hooks.build_cursor_hooks({})
    twice = hooks.build_cursor_hooks(once)
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_write_cursor_hooks_created_then_unchanged(tmp_path):
    assert hooks.write_cursor_hooks(tmp_path) == "created"
    assert hooks.write_cursor_hooks(tmp_path) == "unchanged"


def test_write_cursor_hooks_conflict_on_malformed_json(tmp_path):
    dest = tmp_path / ".cursor" / "hooks.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("[]", encoding="utf-8")  # valid JSON, but not an object
    assert hooks.write_cursor_hooks(tmp_path) == "conflict"
    assert dest.read_text(encoding="utf-8") == "[]"


# --- Real shell execution: the actual escaping, not just string shape --------

def _run_sh(command: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", command], input=stdin, capture_output=True, text=True)


def test_claude_session_start_command_produces_expected_text():
    result = _run_sh(hooks._claude_session_start_command())
    assert result.returncode == 0
    assert "CRITICAL - Code Discovery Protocol" in result.stdout
    assert "\\n" not in result.stdout  # printf must have expanded escapes, not left them literal
    assert result.stdout.count("\n") >= 3


def test_claude_grep_nudge_fires_on_repo_wide_path():
    cmd = hooks._claude_grep_nudge_command()
    result = _run_sh(cmd, stdin=json.dumps({"tool_input": {"path": "."}}))
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "memo code-intelligence index" in payload["hookSpecificOutput"]["additionalContext"]


def test_claude_grep_nudge_silent_on_scoped_path():
    cmd = hooks._claude_grep_nudge_command()
    result = _run_sh(cmd, stdin=json.dumps({"tool_input": {"path": "src/foo.py"}}))
    assert result.returncode == 0
    assert result.stdout == ""  # scoped Grep is exactly the case that shouldn't nag


def test_claude_bash_grep_nudge_fires_on_recursive_grep():
    cmd = hooks._claude_bash_grep_nudge_command()
    result = _run_sh(cmd, stdin=json.dumps({"tool_input": {"command": "grep -rn foo ."}}))
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_claude_bash_grep_nudge_silent_on_non_grep_command():
    cmd = hooks._claude_bash_grep_nudge_command()
    result = _run_sh(cmd, stdin=json.dumps({"tool_input": {"command": "ls -la"}}))
    assert result.returncode == 0
    assert result.stdout == ""


def test_cursor_session_start_command_produces_valid_json():
    result = _run_sh(hooks._cursor_session_start_command())
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "CRITICAL - Code Discovery Protocol" in payload["additional_context"]
    assert "\n" in payload["additional_context"]  # real newlines survived the JSON round-trip


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")
def test_claude_hooks_actually_use_jq_and_it_is_findable():
    # Sanity check that jq (a runtime dependency of the generated hook commands,
    # not of memo itself) is what these tests are actually exercising.
    assert "jq" in hooks._claude_grep_nudge_command()
