"""Opt-in PATH shims — the discoverability-proof half of `memo run`.

`memo run -- pytest ...` only helps if the agent invoking it knows to type that.
memo's own README documents the same risk from the other side (see "Honest limits" —
a live session that never gets nudged toward a mechanism gets zero benefit from it,
indistinguishable from the mechanism being absent). A shim closes that gap for Layer 1
specifically: `memo shim install pytest` puts a thin wrapper named `pytest` ahead of
the real binary on PATH, so *every* invocation of `pytest` — typed by a human, run by
an agent that's never heard of memo, run by GitHub Copilot's agent mode which has no
hook API to register with — gets routed through `memo run` automatically. No opt-in
per invocation is possible to forget, because there's no separate invocation to
remember.

This is deliberately **per-command, opt-in only** — `memo shim install <cmd>` shims
exactly one command; nothing here ever rewrites PATH wholesale. Shims live in
`~/.memo/shims/` (user-level, not per-repo, since PATH ordering is a shell/session
concept, not a project one) and must sit ahead of the real binary regardless of which
project directory a shell happens to be in.

Layout:
    ~/.memo/shims/
        registry.json     # {"shims": {"<cmd>": {"realpath": ..., "installed_at": ...}}}
        <cmd>              # POSIX shell wrapper
        <cmd>.cmd          # Windows cmd.exe wrapper
        <cmd>.ps1          # Windows PowerShell wrapper
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

SHIM_DIR = Path.home() / ".memo" / "shims"
REGISTRY_FILE = SHIM_DIR / "registry.json"

# Names memo refuses to shim: routing `memo run` itself through `memo run` would
# recurse forever the first time the shim fires.
RESERVED_NAMES = {"memo"}


def _load_registry() -> dict:
    if not REGISTRY_FILE.is_file():
        return {"shims": {}}
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"shims": {}}
    data.setdefault("shims", {})
    return data


def _save_registry(data: dict) -> None:
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_real_target(cmd: str) -> str:
    """Find `cmd`'s real executable, skipping memo's own shim directory.

    A plain `shutil.which(cmd)` breaks on reinstall: once a shim is on PATH ahead of
    the real binary, `which` would find the shim itself and record it as its own
    target, recursing forever the next time it runs. Searching PATH by hand and
    skipping SHIM_DIR avoids that.
    """
    try:
        shim_dir_resolved = SHIM_DIR.resolve()
    except OSError:
        shim_dir_resolved = SHIM_DIR

    is_windows = os.name == "nt"
    exts = os.environ.get("PATHEXT", ".EXE;.COM;.BAT;.CMD").split(os.pathsep) if is_windows else [""]

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        d = Path(directory)
        try:
            if d.resolve() == shim_dir_resolved:
                continue
        except OSError:
            pass
        for ext in exts:
            candidate = d / (cmd + ext)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    raise FileNotFoundError(
        f"'{cmd}' not found on PATH outside memo's own shim directory ({SHIM_DIR}).")


def _posix_shim_body(real_target: str) -> str:
    return f'#!/usr/bin/env sh\nexec memo run -- "{real_target}" "$@"\n'


def _cmd_shim_body(real_target: str) -> str:
    return f'@echo off\r\nmemo run -- "{real_target}" %*\r\n'


def _ps1_shim_body(real_target: str) -> str:
    return f'& memo run -- "{real_target}" @args\r\nexit $LASTEXITCODE\r\n'


def install(cmd: str) -> str:
    """Install a PATH shim for `cmd`. Returns the resolved real-target path.

    Idempotent: reinstalling re-resolves the real target (skipping any existing shim)
    and overwrites the shim files, so it's safe to re-run after the underlying tool
    moves (e.g. a venv swap).
    """
    if cmd in RESERVED_NAMES:
        raise ValueError(
            f"refusing to shim '{cmd}': every `memo run` call would recurse into "
            "itself the first time the shim fired.")

    real_target = _resolve_real_target(cmd)

    memo_self = shutil.which("memo")
    if memo_self is not None:
        try:
            if Path(real_target).resolve() == Path(memo_self).resolve():
                raise ValueError(
                    f"refusing to shim '{cmd}': it resolves to memo's own executable.")
        except OSError:
            pass

    SHIM_DIR.mkdir(parents=True, exist_ok=True)

    posix_path = SHIM_DIR / cmd
    posix_path.write_text(_posix_shim_body(real_target), encoding="utf-8", newline="\n")
    posix_path.chmod(posix_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    (SHIM_DIR / f"{cmd}.cmd").write_text(_cmd_shim_body(real_target), encoding="utf-8")
    (SHIM_DIR / f"{cmd}.ps1").write_text(_ps1_shim_body(real_target), encoding="utf-8")

    registry = _load_registry()
    registry["shims"][cmd] = {
        "realpath": real_target,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(registry)
    return real_target


def remove(cmd: str) -> bool:
    """Remove a previously installed shim. Returns False if none was installed."""
    registry = _load_registry()
    had_entry = cmd in registry["shims"]
    registry["shims"].pop(cmd, None)
    _save_registry(registry)

    removed_any = had_entry
    for path in (SHIM_DIR / cmd, SHIM_DIR / f"{cmd}.cmd", SHIM_DIR / f"{cmd}.ps1"):
        if path.exists():
            path.unlink()
            removed_any = True
    return removed_any


def list_() -> dict:
    """Return {"cmd": {"realpath": ..., "installed_at": ...}, ...}."""
    return dict(_load_registry()["shims"])


def is_active(cmd: str) -> bool:
    """True if `cmd` currently resolves (via PATH lookup) into memo's shim dir —
    i.e. the shim is actually in effect, not just recorded in the registry."""
    resolved = shutil.which(cmd)
    if resolved is None:
        return False
    try:
        return Path(resolved).resolve().parent == SHIM_DIR.resolve()
    except OSError:
        return False
