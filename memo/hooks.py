"""Guaranteed instruction delivery via native hook APIs.

Instruction files (`AGENTS.md`, `SKILL.md`, `.mdc`, `copilot-instructions.md`) only
help if the host actually reads them — voluntary, and unverifiable from memo's side.
Measured directly (see this repo's own README, "Honest limits"): an agent that is
never nudged toward its onboarding file can sit next to a fully-indexed repo and get
zero benefit from it, indistinguishable from memo being absent entirely.

Claude Code and Cursor both support hooks that fire unconditionally, independent of
whether an agent would have chosen to read a rule file:

- `SessionStart` (Claude Code) / `sessionStart` (Cursor) inject memo's discovery
  protocol into every session's context, no reading required.
- Claude Code's `PreToolUse` additionally lets us nudge back to memo in real time,
  exactly when a repo-wide `Grep` or `grep -r`-style `Bash` call is about to happen —
  the single most common way memo gets bypassed by an agent that never checked
  `AGENTS.md` in the first place.

GitHub Copilot has no hook API at all — it only ever gets the instruction-file
treatment (`STUB_TEMPLATE`), which is why Layer 1 (`memo run`, `memo shim`) exists as
a PATH-level mechanism instead of depending on Copilot noticing anything.

**Cursor's `beforeShellExecution` is deliberately NOT used for a live nudge.** Its
permission model is allow/ask/deny, and per Cursor's own docs, attaching guidance to
the agent without blocking appears to require `permission: "ask"` — which interrupts
the user for approval. Firing that on every grep would be worse than doing nothing,
so only Cursor's `sessionStart` (unconditional, non-blocking by design) is written
here. Revisit if Cursor's docs later confirm a genuinely non-blocking guidance path.

Both writers are idempotent and non-destructive: memo's own hook entries are tagged
with `_MARKER` in their command string, so re-running `memo init` replaces exactly
those entries in place — every other hook, permission, or setting already in the file
is left completely untouched. A file that exists but isn't valid JSON is never
touched at all (surfaced as a "conflict" the same way a hand-edited rule file is).
"""

from __future__ import annotations

import json
from pathlib import Path

_MARKER = "MEMO_MANAGED_HOOK_V1"

_DISCOVERY_TEXT = (
    "CRITICAL - Code Discovery Protocol for this repo:\n"
    "1. ALWAYS use `python -m memo.cli <cmd>` FIRST for any question spanning files "
    "or relationships: callers, impact/blast-radius, trace, test coverage, map, "
    "find, architecture, or \"how does X work\".\n"
    "2. Use Read/Grep/Glob freely for a single already-known file, configs, or "
    "non-code text.\n"
    "3. Full command table: AGENTS.md or `python -m memo.cli guide`.\n"
)

_GREP_NUDGE_TEXT = (
    "Repo-wide search detected. This repo has a memo code-intelligence index — "
    "`python -m memo.cli map/find/callers/brief` may answer this from the index "
    "without scanning files. See AGENTS.md."
)


def _is_memo_command(cmd: str | None) -> bool:
    return bool(cmd) and _MARKER in cmd


def _tagged(body: str) -> str:
    return f": {_MARKER}\n{body}"


def _shell_single_quote(s: str) -> str:
    """Shell-safe single-quoted literal (POSIX sh)."""
    return "'" + s.replace("'", "'\\''") + "'"


def _printf_escaped(text: str) -> str:
    """`printf`'s format string interprets backslash escapes itself — a literal
    newline in `text` must become the two characters `\\n` for printf to render it
    as a newline, not embed a raw newline that breaks the single-quoted literal."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _load_json(path: Path) -> dict | None:
    """{} if absent (safe to create fresh), a dict if valid JSON, None if the file
    exists but isn't parseable/isn't an object (caller must not touch it)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(dest: Path, data: dict, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


# --- Claude Code -------------------------------------------------------------

def _claude_additional_context_command(text: str) -> str:
    payload = json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text},
    })
    return f"printf %s {_shell_single_quote(payload)}"


def _claude_session_start_command() -> str:
    return f"printf '{_printf_escaped(_DISCOVERY_TEXT)}'"


def _claude_grep_nudge_command() -> str:
    # A Grep scoped to one known path is exactly the case memo's own guide says is
    # fine to do directly — only nudge on a repo-wide scan (no `path`, defaults to ".").
    inner = _claude_additional_context_command(_GREP_NUDGE_TEXT)
    return (
        "jq -r '.tool_input.path // \".\"' | { read -r p; if [ \"$p\" = \".\" ]; then "
        f"{inner}; fi; }}"
    )


def _claude_bash_grep_nudge_command() -> str:
    inner = _claude_additional_context_command(_GREP_NUDGE_TEXT)
    return (
        "jq -r '.tool_input.command // \"\"' | { read -r cmd; case \"$cmd\" in "
        f"*grep*-r*|*grep*-R*) {inner};; esac; }}"
    )


def _upsert_claude_hook_group(existing: list, matcher: str | None, command: str) -> list:
    """Replace any existing memo-managed entry in this matcher's slot with a fresh
    one; every other (non-memo, or different-matcher) entry passes through unchanged."""
    kept = []
    for entry in existing:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        if entry.get("matcher") == matcher:
            inner = [h for h in entry.get("hooks", [])
                     if isinstance(h, dict) and not _is_memo_command(h.get("command"))]
            if inner:
                kept.append({**entry, "hooks": inner})
            # else: entry was entirely memo's own — drop it, re-added below.
        else:
            kept.append(entry)
    new_entry: dict = {"hooks": [{"type": "command", "command": _tagged(command)}]}
    if matcher is not None:
        new_entry = {"matcher": matcher, **new_entry}
    kept.append(new_entry)
    return kept


def build_claude_settings(existing: dict) -> dict:
    settings = dict(existing)
    hooks = dict(settings.get("hooks", {}))
    hooks["SessionStart"] = _upsert_claude_hook_group(
        hooks.get("SessionStart", []) if isinstance(hooks.get("SessionStart"), list) else [],
        matcher=None, command=_claude_session_start_command())
    pre = hooks.get("PreToolUse", []) if isinstance(hooks.get("PreToolUse"), list) else []
    pre = _upsert_claude_hook_group(pre, matcher="Grep", command=_claude_grep_nudge_command())
    pre = _upsert_claude_hook_group(pre, matcher="Bash", command=_claude_bash_grep_nudge_command())
    hooks["PreToolUse"] = pre
    settings["hooks"] = hooks
    return settings


def write_claude_code_hooks(repo: Path, dry_run: bool = False) -> str:
    dest = repo / ".claude" / "settings.json"
    existing = _load_json(dest)
    if existing is None:
        return "conflict"
    updated = build_claude_settings(existing)
    if json.dumps(existing, sort_keys=True) == json.dumps(updated, sort_keys=True):
        return "unchanged"
    status = "created" if not dest.is_file() else "updated"
    _write_json(dest, updated, dry_run)
    return status


# --- Cursor --------------------------------------------------------------------

def _cursor_session_start_command() -> str:
    payload = json.dumps({"additional_context": _DISCOVERY_TEXT})
    body = f"printf %s {_shell_single_quote(payload)}"
    return f"sh -c {_shell_single_quote(_tagged(body))}"


def _upsert_cursor_hook_list(existing: list, command: str) -> list:
    kept = [h for h in existing
            if isinstance(h, dict) and not _is_memo_command(h.get("command"))]
    kept.append({"command": command})
    return kept


def build_cursor_hooks(existing: dict) -> dict:
    config = dict(existing)
    config.setdefault("version", 1)
    hooks = dict(config.get("hooks", {}))
    hooks["sessionStart"] = _upsert_cursor_hook_list(
        hooks.get("sessionStart", []) if isinstance(hooks.get("sessionStart"), list) else [],
        command=_cursor_session_start_command())
    config["hooks"] = hooks
    return config


def write_cursor_hooks(repo: Path, dry_run: bool = False) -> str:
    dest = repo / ".cursor" / "hooks.json"
    existing = _load_json(dest)
    if existing is None:
        return "conflict"
    updated = build_cursor_hooks(existing)
    if json.dumps(existing, sort_keys=True) == json.dumps(updated, sort_keys=True):
        return "unchanged"
    status = "created" if not dest.is_file() else "updated"
    _write_json(dest, updated, dry_run)
    return status
