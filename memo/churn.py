"""`memo churn` — commit frequency, author count, and recency per file: the risk
signal memo's index has never captured, because it's a pure snapshot of current
source. Everything else in `.memo/` answers "what does the code look like now";
this is the one command that answers "how much has this been touched, and by whom."

Deliberately a single `git log --name-only` walk, not one `git log` per file — a
1,000-commit history times 500 files would be 500 subprocess calls otherwise. One
call, parsed once, matches the pattern `insights.changed()` already uses for git.

`commits × raw_tokens` is offered as a rough "risk" ranking (churn × size, since memo
has no real complexity metric — see the README's own honesty about that) — labeled a
proxy, not a fact, the same way `arch`'s hotspot counts now carry a "verify before
trusting" caveat after being found to overcount for common names.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .cache import CacheEntry
from .mapper import common_root, disambiguate

_COMMIT_HEADER = re.compile(r"^@@(?P<hash>[0-9a-f]+)\|(?P<author>.*)\|(?P<ts>\d+)$")


def _git_toplevel(root: str) -> tuple[str | None, str | None]:
    r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None, (r.stderr or "not a git repository").strip()
    return r.stdout.strip().replace("\\", "/"), None


def compute(entries: list[CacheEntry], since: str = "1 year ago", limit: int = 0) -> dict:
    """Per-file commit count, distinct author count, and most recent commit time,
    over `since` (git's own date syntax — "1 year ago", "90 days ago", "all" for the
    full history). Restricted to cached files: churn on a file memo doesn't index
    isn't actionable through memo's own commands anyway.
    """
    if not entries:
        return {"error": "no cached files", "files": []}
    root = common_root([e.path for e in entries])
    git_root, err = _git_toplevel(root)
    if err:
        return {"error": err, "files": []}

    args = ["git", "-C", root, "log", "--name-only",
            "--pretty=format:@@%H|%an|%at"]
    if since != "all":
        args += [f"--since={since}"]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return {"error": (r.stderr or "git log failed").strip(), "files": []}

    cached_by_relpath: dict[str, CacheEntry] = {}
    for e in entries:
        rel = e.path.replace("\\", "/")
        if git_root and rel.startswith(git_root):
            rel = rel[len(git_root):].lstrip("/")
        cached_by_relpath[rel] = e

    commits: dict[str, int] = {}
    authors: dict[str, set] = {}
    last_touched: dict[str, int] = {}
    author, ts = None, 0
    for line in r.stdout.splitlines():
        m = _COMMIT_HEADER.match(line)
        if m:
            author, ts = m.group("author"), int(m.group("ts"))
            continue
        path = line.strip()
        if not path or path not in cached_by_relpath:
            continue
        commits[path] = commits.get(path, 0) + 1
        authors.setdefault(path, set()).add(author)
        last_touched[path] = max(last_touched.get(path, 0), ts)

    rows = []
    for rel, e in cached_by_relpath.items():
        c = commits.get(rel, 0)
        if c == 0:
            continue  # untouched in the window — not evidence of anything, just quiet
        rows.append({
            "path": e.path, "commits": c, "authors": len(authors.get(rel, ())),
            "last_touched": last_touched.get(rel, 0),
            "raw_tokens": e.raw_tokens,
            "risk_score": c * e.raw_tokens,  # a proxy, not a metric — see module docstring
        })
    rows.sort(key=lambda x: -x["risk_score"])
    if limit:
        rows = rows[:limit]
    return {"error": None, "since": since, "files": rows,
            "total_files_touched": len(rows)}


def render(result: dict, terse: bool = False, limit: int = 30) -> str:
    if result.get("error"):
        return f'_git error: {result["error"]}_\n'
    rows = result["files"][:limit]
    if terse:
        lines = [f"churn (since {result['since']}, terse) — {result['total_files_touched']} file(s) touched"]
        lbl = disambiguate([r["path"] for r in rows])
        for r in rows:
            lines.append(f"{lbl[r['path']]} commits={r['commits']} authors={r['authors']} "
                         f"risk={r['risk_score']}")
        if not rows:
            lines.append("(no commits in this window)")
        return "\n".join(lines) + "\n"

    lines = [f"# Churn since {result['since']} — {result['total_files_touched']} cached file(s) touched", ""]
    lines.append("_`risk` = commits × raw tokens — a proxy for \"this file changes a lot "
                 "and is large,\" not a real complexity measure (memo has no complexity "
                 "metric; it does no analysis beyond parsing). Highest-risk first._")
    lines.append("")
    if not rows:
        lines.append(f"_No commits touching a cached file in the last {result['since']}._")
        return "\n".join(lines).rstrip() + "\n"
    lbl = disambiguate([r["path"] for r in rows])
    for r in rows:
        lines.append(f"- **{lbl[r['path']]}** — {r['commits']} commit(s), "
                     f"{r['authors']} author(s), risk={r['risk_score']}")
    if len(result["files"]) > limit:
        lines.append(f"\n_…and {len(result['files']) - limit} more file(s) — raise --limit._")
    return "\n".join(lines).rstrip() + "\n"
