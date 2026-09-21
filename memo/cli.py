"""memo — CLI entry point.

Commands:
    memo cache <file>              Summarize a file and store it.
    memo get <file>                Print cached summary; regenerate if stale/missing.
    memo cache-dir <dir> [-r]      Batch-cache source files in a directory.
    memo run -- <cmd> [args]       Run a command, compress its stdout for an agent, keep the original recoverable.
    memo recall <hash>             Print the untruncated original behind a `run` compression trailer.
    memo shim install/remove/list  Opt-in PATH shim so <cmd> is auto-routed through `memo run`.
    memo map <query>               Routing digest (multi-term = AND co-occurrence).
    memo find <value>              Value/literal search, hits attributed to enclosing symbol.
    memo callers <symbol>          Methods that call <symbol> (candidate, name-based).
    memo calls <symbol>            Project symbols invoked inside <symbol>'s body.
    memo trace <symbol> --up/--down  Walk the call chain N levels.
    memo query <expr>              Ad-hoc predicate filter over symbols/edges.
    memo schema                    Describe the graph's fields, tiers, edge kinds.
    memo ingest-traces             Merge observed runtime calls in as verified edges.
    memo adr add/list/show/for     Architecture Decision Records linked to symbols.
    memo register/link/registry    Cross-repo service registry + declared links.
    memo trace <symbol> --cross-service  Follow a trace across linked repos.
    memo show                      List cached files with token counts + ratio.
    memo ui                        Local read-only dashboard over the index (127.0.0.1).
    memo clear [file]              Clear one file's cache, or everything.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from . import __version__
import json as _json

from . import console, render
from .cache import Cache, CacheEntry, normalize_path, sha256_of_bytes, sha256_of_file
from .config import (
    CACHE_DIRNAME, IGNORE_FILENAME, find_cache_root, language_for, load_extensions,
)
from .console import SYM, init_streams
from .guide import GUIDE
from .targets import ALL, TARGETS, resolve
from .graph import (
    brief, find_callers, find_calls, find_field_refs, find_value, find_value_wide,
    neighbors, peek, peek_at, render_brief, render_callers, render_calls, render_find,
    render_field_refs, render_neighbors, render_peek, render_peek_at, render_trace,
    trace,
)
from . import graphindex
from . import hooks as hooks_mod
from .insights import (
    arch, changed, covering_tests, deadcode, impact, render_arch, render_changed,
    render_deadcode, render_impact, render_routes, render_tests, routes as routes_fn,
)
from .ignore import DEFAULT_MEMOIGNORE, is_ignored, load_ignore_spec
from .mapper import build_map, render_map
from .stacktrace import render_stack, resolve_stack
from .summarizer import render_markdown, summarize
from .tokens import count_tokens, encoding_available
from . import adr as adr_mod
from . import churn as churn_mod
from . import clusters as clusters_mod
from . import query as query_mod
from . import runner as runner_mod
from . import services
from . import shim as shim_mod
from . import traces as traces_mod


def _json_out(obj) -> str:
    """Serialize `--json` output compactly, not pretty-printed.

    `--json` exists for programmatic consumption (an agent's own parser, or a script) —
    the human-readable path is the default terse/full render(), which every command
    already has. `indent=2` therefore buys nothing but whitespace, and it is not free
    whitespace: measured on this repo, `arch --json` is 2043 tokens pretty-printed vs.
    1493 compact (-26.9%), and `impact <sym> --json` on a wide blast radius is 11012 vs.
    8737 (-20.7%). `json.loads` does not care about formatting either way, so this is
    the same class of fix as dropping an uninformative confidence marker: real tokens,
    zero information lost.
    """
    return _json.dumps(obj, separators=(",", ":"))


def _load_cache_or_die() -> "Cache":
    c = Cache(find_cache_root())
    if not c.list_entries():
        raise click.ClickException(
            "Cache is empty. Run `memo cache-dir <repo> --recursive` first."
        )
    return c


def _graph_staleness(g, entries) -> str | None:
    """Why the graph no longer matches the cache, or None if it does.

    The original check compared only file *counts*, which cannot see an edit that leaves
    the count unchanged — editing a file in place, or re-running `memo cache` on an
    already-indexed one. Both leave the graph holding old symbol lines, and because the
    rule files tell agents to cite memo's file:line without re-reading to confirm,
    nothing downstream catches it.

    Costs no extra IO: `entries` is already loaded by the caller.
    """
    if g is None:
        return None
    if g.fingerprint:
        if graphindex.fingerprint_of(entries) != g.fingerprint:
            return ("the indexed files or their contents have changed since the graph "
                    "was built")
        return None
    # Graph predates the fingerprint field. Fall back to the count rather than treating
    # an absent fingerprint as a mismatch, which would warn on every query.
    if g.files and g.files != len(entries):
        return (f"graph was built from {g.files} file(s) but the cache now holds "
                f"{len(entries)}")
    return None


def _load_graph_or_die():
    c = _load_cache_or_die()
    g = graphindex.load_or_build(c)
    entries = c.list_entries()
    n_files = len(entries)
    # The stale-graph warning below stays unconditional — a wrong index must never be
    # silent (that bit us once). What used to print here as well was an *unconditional*
    # info line naming the cache root, file count and symbol count, on every graph
    # command.
    #
    # Two reasons it had to go. Agent harnesses fold stderr into the conversation, so a
    # healthy run paid for a diagnostic nobody asked for on every call — and paid for it
    # in the position that gets re-sent on every later model turn. It also carried counts
    # that move whenever the index moves, so two otherwise identical answers differed
    # byte-for-byte and could not be served from a prompt cache.
    #
    # MEMO_VERBOSE=1 brings it back for debugging which cache is actually in play.
    if os.environ.get("MEMO_VERBOSE"):
        click.echo(f"memo: cache {c.root} — {n_files} files, graph {len(g.symbols)} symbols",
                   err=True)
    reason = _graph_staleness(g, entries)
    if reason:
        click.echo(
            click.style(
                f"warning: {reason} — results may cite lines that have moved. "
                f"Refresh with `memo cache-dir <repo> --recursive`.",
                fg="yellow"),
            err=True)
    return c, g


def _read_text(path: Path) -> str:
    """Read a file as text, tolerating odd encodings."""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _build_entry(path: Path, language: str) -> CacheEntry:
    """Run static analysis and package a CacheEntry (does not persist)."""
    raw_bytes = path.read_bytes()
    text = _read_text(path)
    fs = summarize(text, language)
    raw_tokens, exact_a = count_tokens(text)
    # Render once with placeholder, count, then re-render (token line is stable enough).
    summary_text = render_markdown(fs, path, raw_tokens, 0)
    summary_tokens, exact_b = count_tokens(summary_text)
    summary_text = render_markdown(fs, path, raw_tokens, summary_tokens)
    return CacheEntry(
        path=str(path.resolve()),
        content_hash=sha256_of_bytes(raw_bytes),
        language=language,
        summary_text=summary_text,
        structured=fs.to_dict(),
        raw_tokens=raw_tokens,
        summary_tokens=summary_tokens,
        tokens_exact=exact_a and exact_b,
    )


def _cache_one(cache: Cache, path: Path, extensions: dict) -> CacheEntry | None:
    """Summarize + store a single file. Returns the entry, or None if unsupported."""
    language = language_for(path, extensions)
    if language is None:
        return None
    entry = _build_entry(path, language)
    cache.store(entry)
    return entry


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="memo")
def main() -> None:
    """memo — local, offline file-summary cache for AI coding assistants.

    Summaries come from local static analysis (Python AST + heuristic parsers for
    C#, VB.NET, JS/TS, and ASPX markup). No LLM API is used.
    """
    # Must run before any output: agents capture our stdout through a pipe, where
    # the locale encoding (cp1252 on Windows) can't represent memo's glyphs.
    init_streams()


@main.command()
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def cache(file_path: Path) -> None:
    """Summarize FILE_PATH and store it in the local cache."""
    root = find_cache_root()
    extensions = load_extensions(root)
    c = Cache(root)
    language = language_for(file_path, extensions)
    if language is None:
        raise click.ClickException(
            f"'{file_path.name}' has an unsupported extension "
            f"({file_path.suffix or 'none'}). Supported: {', '.join(sorted(extensions))}"
        )
    entry = _build_entry(file_path, language)
    c.store(entry)
    ratio = (entry.raw_tokens / entry.summary_tokens) if entry.summary_tokens else 0
    click.echo(entry.summary_text)
    click.echo(
        click.style(
            f"\n[cached: {file_path.name} — {entry.raw_tokens}{SYM['arrow']}{entry.summary_tokens} tokens, "
            f"{ratio:.1f}x smaller]",
            fg="green",
        ),
        err=True,
    )


@main.command()
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def get(file_path: Path) -> None:
    """Print the cached summary for FILE_PATH; regenerate if changed or missing."""
    root = find_cache_root()
    extensions = load_extensions(root)
    c = Cache(root)

    if c.is_fresh(file_path):
        entry = c.get_by_path(file_path)
        if entry is not None:
            click.echo(entry.summary_text)
            click.echo(click.style("[cache hit]", fg="cyan"), err=True)
            return

    # Stale or missing -> regenerate (same path as `memo cache`).
    language = language_for(file_path, extensions)
    if language is None:
        raise click.ClickException(
            f"'{file_path.name}' has an unsupported extension "
            f"({file_path.suffix or 'none'}). Supported: {', '.join(sorted(extensions))}"
        )
    entry = _build_entry(file_path, language)
    c.store(entry)
    click.echo(entry.summary_text)
    click.echo(click.style("[regenerated — file changed or not cached]", fg="yellow"), err=True)


def _walk_sources(directory: Path, spec, recursive: bool) -> list[Path]:
    """Enumerate candidate files, pruning ignored directories instead of descending.

    `rglob("*")` visits everything and leaves filtering to the caller, so a repo with
    node_modules/ or packages/ pays to enumerate tens of thousands of files it will
    immediately discard (26,204 visited for 541 real source files in one test repo).
    Skipping those directories outright is the difference between a ~4s walk and a
    near-instant one. File-level patterns are still applied per file by the caller.
    """
    if not recursive:
        return sorted(p for p in directory.glob("*") if p.is_file())

    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        here = Path(dirpath)
        keep = []
        for name in dirnames:
            if name == CACHE_DIRNAME:
                continue
            if spec is not None:
                try:
                    rel = (here / name).relative_to(directory).as_posix()
                except ValueError:
                    rel = name
                # Trailing slash so gitignore directory patterns (`node_modules/`) match.
                if spec.match_file(rel + "/"):
                    continue
            keep.append(name)
        dirnames[:] = keep  # in-place: this is how os.walk is told not to descend
        out.extend(here / n for n in filenames)
    return sorted(out)


def _index_directory(directory: Path, recursive: bool, *, quiet: bool = False,
                     force: bool = False) -> dict:
    """Summarize every supported file under `directory`, then rebuild the code graph.

    Shared by `cache-dir` and `init` so there is exactly one indexing path. Per-file
    progress goes to stderr: stdout is reserved for command *output* (and must stay
    clean for `--json` consumers), while progress is diagnostic chatter.

    Unchanged files are skipped by default — re-analyzing a file whose content hash
    and analyzer version both match cannot produce a different result. Pass
    force=True to re-summarize everything regardless.
    """
    root = find_cache_root()
    extensions = load_extensions(root)
    c = Cache(root)
    # A .memoignore in the directory being indexed wins; otherwise fall back to the
    # repo root (the directory that contains .memo), which is where it usually lives.
    spec = load_ignore_spec(directory) or load_ignore_spec(root.parent)

    files = _walk_sources(directory, spec, recursive)
    cached = skipped_ext = skipped_ignore = failed = reused = 0
    total_raw = total_sum = 0

    # One index read up front, so the unchanged-file check costs no extra IO per file.
    index = c.index_snapshot()
    known_versions = {}
    if not force:
        # Keyed off the index so every indexed path is covered, with the analyzer
        # version resolved via content hash (entries are stored per hash, not per path).
        ver_by_hash = {e.content_hash: e.memo_version for e in c.list_entries()}
        known_versions = {path: (rec["hash"], ver_by_hash.get(rec["hash"], ""))
                          for path, rec in index.items()}

    # Never walk memo's own cache. It holds thousands of JSON files in a large repo, and
    # relying on a .memoignore entry means a repo without one pays to stat all of them.
    candidates = [p for p in sorted(files)
                  if p.is_file() and CACHE_DIRNAME not in p.parts]

    # Indexing a large repo is a multi-second silent wait otherwise, which reads as a
    # hang. With per-file lines suppressed, show a bar instead — but only on a real
    # terminal, so captured/redirected output stays clean.
    import contextlib
    import sys as _sys

    if quiet and _sys.stderr.isatty():
        tracker = click.progressbar(candidates, label="Indexing", file=_sys.stderr)
    else:
        tracker = contextlib.nullcontext(candidates)

    seen: set[str] = set()
    with c.batch(), tracker as tracked:
        for path in tracked:
            if is_ignored(spec, directory, path):
                skipped_ignore += 1
                continue
            if language_for(path, extensions) is None:
                skipped_ext += 1
                continue
            try:
                key = normalize_path(path)
                seen.add(key)
                prev = known_versions.get(key)
                if prev is not None and prev[1] == __version__ \
                        and prev[0] == sha256_of_file(path):
                    reused += 1
                    # Keep the existing index record; nothing about it changed.
                    if key in index:
                        c.keep(key, index[key])
                    continue
                entry = _cache_one(c, path, extensions)
            except Exception as exc:  # keep going on a single bad file
                failed += 1
                click.echo(click.style(f"  {SYM['fail']} {path}: "
                                       f"{type(exc).__name__}: {exc}", fg="red"), err=True)
                continue
            if entry is not None:
                cached += 1
                total_raw += entry.raw_tokens
                total_sum += entry.summary_tokens
                if not quiet:
                    click.echo(
                        f"  {SYM['ok']} {path.relative_to(directory)} "
                        f"({entry.raw_tokens}{SYM['arrow']}{entry.summary_tokens} tok)",
                        err=True,
                    )

        # Evict anything under `directory` that is no longer indexable — newly ignored,
        # deleted, or renamed. Done inside the batch so it lands in the single index write.
        pruned = len(c.prune(str(directory.resolve()), seen))

    # After the index is committed, reclaim entry files nothing references any more.
    collected = c.gc()

    stats = {
        "pruned": pruned, "collected": collected,
        "cached": cached, "reused": reused, "skipped_ext": skipped_ext,
        "skipped_ignore": skipped_ignore, "failed": failed,
        "raw_tokens": total_raw, "summary_tokens": total_sum,
        "symbols": 0, "edges": 0,
    }

    # Rebuild the consolidated manifest so the next query is a single read, and the
    # persistent code graph that powers callers/calls/trace/arch/impact.
    if cached or reused:
        # The batch just rewrote index.json, so the old manifest no longer validates;
        # list_entries() falls back to per-file reads and rewrites the manifest itself.
        entries = c.list_entries()
        try:
            g = graphindex.build(entries, cache_root=root)
            graphindex.save(g, root)
            stats["symbols"], stats["edges"] = len(g.symbols), len(g.call_sites)
        except Exception as exc:
            click.echo(click.style(f"  {SYM['fail']} graph build failed: {exc}", fg="red"),
                       err=True)
    return stats


def _report_index(stats: dict) -> None:
    """Print the human summary of an indexing run."""
    ratio = (stats["raw_tokens"] / stats["summary_tokens"]) if stats["summary_tokens"] else 0
    reused = stats.get("reused", 0)
    pruned = stats.get("pruned", 0)
    click.echo(
        click.style(
            f"Indexed {stats['cached']} file(s)"
            + (f", reused {reused} unchanged" if reused else "")
            + (f", pruned {pruned} no-longer-indexable" if pruned else "")
            + (f", reclaimed {stats['collected']} orphaned entry file(s)"
               if stats.get("collected") else "")
            + f". Skipped {stats['skipped_ext']} (extension) "
            f"+ {stats['skipped_ignore']} (.memoignore). Failed {stats['failed']}.",
            fg="green",
        )
    )
    if stats["cached"]:
        click.echo(f"Tokens: {stats['raw_tokens']}{SYM['arrow']}{stats['summary_tokens']} "
                   f"({ratio:.1f}x smaller).")
    if stats["symbols"]:
        click.echo(f"Graph: {stats['symbols']} symbols, {stats['edges']} call edges.")


@main.command(name="cache-dir")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-r", "--recursive", is_flag=True, help="Recurse into subdirectories.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress per-file progress.")
@click.option("--force", is_flag=True,
              help="Re-summarize every file, even ones whose content hasn't changed.")
def cache_dir(directory: Path, recursive: bool, quiet: bool, force: bool) -> None:
    """Batch-cache all supported source files in DIRECTORY.

    Incremental by default: files whose content hash and analyzer version both match
    the cache are reused rather than re-analyzed. Use --force to redo everything.

    Skips files matched by a .memoignore (gitignore syntax) and any extension not
    on the allowlist (binaries, images, lockfiles, etc.).
    """
    _report_index(_index_directory(directory, recursive, quiet=quiet, force=force))


# --- init: one-command onboarding -------------------------------------------

_STATUS_STYLE = {
    "created": ("green", "wrote"),
    "updated": ("green", "updated"),
    "unchanged": ("cyan", "unchanged"),
    "conflict": ("yellow", "SKIPPED (hand-edited)"),
}


def _apply(dest: Path, new_text: str, status: str, dry_run: bool) -> None:
    if status in ("created", "updated") and not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps generated files byte-identical across platforms, so
        # re-running init on Windows doesn't show up as a diff for Linux teammates.
        dest.write_text(new_text, encoding="utf-8", newline="\n")


def _write_target(repo: Path, target, force: bool, dry_run: bool) -> str:
    dest = repo / target.rel_path
    old = dest.read_text(encoding="utf-8") if dest.is_file() else None
    if target.exclusive:
        # memo owns this file outright (Cursor .mdc needs frontmatter on line 1).
        new_text = render.file_text(target)
        status = "unchanged" if old == new_text else ("updated" if old is not None else "created")
    else:
        new_text, status = render.upsert(old, render.render_body(target), force=force)
    _apply(dest, new_text, status, dry_run)
    return status


def _ensure_line(repo: Path, filename: str, line: str, dry_run: bool) -> str:
    """Append `line` to `filename` if no existing line already says the same thing."""
    dest = repo / filename
    old = dest.read_text(encoding="utf-8") if dest.is_file() else None
    if old is not None:
        present = {ln.strip().rstrip("/") for ln in old.splitlines()}
        if line.strip().rstrip("/") in present:
            return "unchanged"
        sep = "" if old.endswith("\n") else "\n"
        new_text, status = old + sep + line + "\n", "updated"
    else:
        new_text, status = line + "\n", "created"
    _apply(dest, new_text, status, dry_run)
    return status


def _ensure_memoignore(repo: Path, dry_run: bool) -> str:
    """Seed a .memoignore, but never overwrite one the user already tuned."""
    dest = repo / IGNORE_FILENAME
    if dest.is_file():
        return "unchanged"
    _apply(dest, DEFAULT_MEMOIGNORE, "created", dry_run)
    return "created"


@main.command(name="init")
@click.option("--agent", "agent_keys", default="all", show_default=True,
              help="Which rule files to write: comma-separated "
                   "agents,claude,cursor,copilot — or 'all'.")
@click.option("--index/--no-index", "do_index", default=True, show_default=True,
              help="Build the .memo index after writing the rule files.")
@click.option("--force", is_flag=True,
              help="Replace a managed block even if it was edited by hand.")
@click.option("--print", "print_only", is_flag=True,
              help="Print the rendered rule files to stdout and exit; write nothing.")
@click.option("--dry-run", is_flag=True, help="Report what would change without writing.")
@click.option("--hooks/--no-hooks", default=True, show_default=True,
              help="Also install Claude Code/Cursor hooks for guaranteed delivery of "
                   "the discovery protocol — fires unconditionally at session start, "
                   "not dependent on the agent choosing to read AGENTS.md. Written "
                   "only for the agents actually selected via --agent.")
@click.option("--path", "repo_arg", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Repo root to set up [default: current directory].")
def init_cmd(agent_keys: str, do_index: bool, force: bool, print_only: bool,
             dry_run: bool, hooks: bool, repo_arg: Path | None) -> None:
    """Set this repo up for agentic IDEs: write rule files and build the index.

    Replaces the old manual checklist (copy three templates to three paths, edit
    .gitignore, run cache-dir). Safe to re-run: memo only rewrites its own delimited
    block in each file and leaves your content untouched.
    """
    repo = (repo_arg or Path.cwd()).resolve()

    keys = list(ALL) if agent_keys.strip().lower() == "all" else [
        k.strip().lower() for k in agent_keys.split(",") if k.strip()
    ]
    unknown = [k for k in keys if k not in TARGETS]
    if unknown:
        raise click.ClickException(
            f"Unknown agent(s): {', '.join(unknown)}. Choose from: {', '.join(ALL)}, or 'all'."
        )
    selected = resolve(keys)

    if print_only:
        for t in selected:
            click.echo(f"===== {t.rel_path} =====")
            click.echo(render.file_text(t) if t.exclusive
                       else render.wrap(render.render_body(t)))
            click.echo()
        return

    # Pin the cache location so every downstream call agrees on the repo root, even
    # when an ancestor directory happens to have its own .memo.
    os.environ["MEMO_HOME"] = str(repo)

    click.echo(f"memo init {SYM['arrow']} {repo}")
    if dry_run:
        click.echo(click.style("(dry run — nothing will be written)", fg="yellow"))
    click.echo()

    added_dep = [t.key for t in selected if t.key not in keys]
    conflicts = []

    click.echo("Rule files:")
    for t in selected:
        status = _write_target(repo, t, force, dry_run)
        colour, verb = _STATUS_STYLE[status]
        if status == "conflict":
            conflicts.append(t.rel_path)
        click.echo(f"  {click.style(verb.ljust(22), fg=colour)} {t.rel_path}"
                   f"{'  — ' + t.note if t.note else ''}")
    if added_dep:
        click.echo(click.style(
            f"  (added {', '.join(added_dep)} — required by a selected target)", fg="cyan"))

    click.echo("\nRepo config:")
    for label, status in (
        (".gitignore (.memo/)", _ensure_line(repo, ".gitignore", ".memo/", dry_run)),
        (IGNORE_FILENAME, _ensure_memoignore(repo, dry_run)),
    ):
        colour, verb = _STATUS_STYLE[status]
        click.echo(f"  {click.style(verb.ljust(22), fg=colour)} {label}")

    selected_keys = {t.key for t in selected}
    hook_writers = [
        (key, label, fn) for key, label, fn in (
            ("claude", ".claude/settings.json",
             lambda: hooks_mod.write_claude_code_hooks(repo, dry_run)),
            ("cursor", ".cursor/hooks.json",
             lambda: hooks_mod.write_cursor_hooks(repo, dry_run)),
        ) if key in selected_keys
    ]
    if hooks and hook_writers:
        click.echo("\nHooks (guaranteed delivery — fires whether or not the agent "
                   "reads AGENTS.md):")
        for _key, label, fn in hook_writers:
            status = fn()
            colour, verb = _STATUS_STYLE[status]
            click.echo(f"  {click.style(verb.ljust(22), fg=colour)} {label}")
            if status == "conflict":
                click.echo(click.style(
                    f"      {label} isn't valid JSON — left untouched. Fix it or "
                    "add memo's hooks manually (see `memo/hooks.py` for the exact "
                    "SessionStart/PreToolUse entries).", fg="yellow"))
    elif not hooks:
        click.echo("\nHooks: skipped (--no-hooks)")

    if do_index and not dry_run:
        click.echo("\nIndexing (this is the slow part; progress on stderr)...")
        _report_index(_index_directory(repo, recursive=True, quiet=True))
    elif do_index:
        click.echo("\nWould index: " + str(repo))

    click.echo()
    if conflicts:
        click.echo(click.style(
            f"{SYM['warn']} Left alone (memo's block was edited by hand): "
            f"{', '.join(conflicts)}\n  Re-run with --force to replace it.", fg="yellow"))
    click.echo(click.style("Done. Next:", bold=True))
    click.echo("  1. Restart your IDE (or reload rules) so it picks up the new files.")
    click.echo("  2. Try:  python -m memo.cli arch --terse")
    click.echo("  3. Check freshness later with:  python -m memo.cli status")


@main.command(name="map")
@click.argument("query")
@click.option("--no-refs", is_flag=True, help="Skip the on-disk reference scan (definitions only, no file IO).")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw digest as JSON (for programmatic use).")
@click.option("--terse", is_flag=True, help="Coordinate-only output (path:line symbol) — minimal tokens.")
@click.option("--limit", default=25, show_default=True, help="Max rows per section.")
def map_cmd(query: str, no_refs: bool, as_json: bool, terse: bool, limit: int) -> None:
    """Routing digest for QUERY: where it's defined, what references it, likely entry points.

    Answers "which files define/use <symbol-or-concept>" from the cache in a few
    hundred tokens — definitions come from cached structured symbols; references
    are counted by re-scanning the cached files on disk. Run `memo cache-dir`
    first to populate the cache.
    """
    c = Cache(find_cache_root())
    entries = c.list_entries()
    if not entries:
        raise click.ClickException(
            "Cache is empty. Run `memo cache-dir <repo> --recursive` first."
        )
    result = build_map(entries, query, scan_refs=not no_refs)
    if as_json:
        click.echo(_json_out(result))
        return
    out = render_map(result, limit=limit, terse=terse)
    # A cheap, additive lookup — only costs anything when an ADR actually governs this
    # symbol, so it never taxes the common case of a query with no recorded decisions.
    adrs = adr_mod.for_symbol(find_cache_root(), query)
    if adrs:
        tag = ", ".join(f"ADR-{a['id']}" for a in adrs)
        out += (f"\n{tag}: {'; '.join(a['title'] for a in adrs)}" if terse
               else f"\n_Governed by {tag} — see `memo adr for {query}`._\n")
    click.echo(out)


@main.command()
@click.argument("value")
@click.option("--regex", is_flag=True, help="Treat VALUE as a regular expression.")
@click.option("--case-sensitive", is_flag=True, help="Case-sensitive match.")
@click.option("--all", "scan_all", is_flag=True,
              help="Also scan non-cached files (docs, config, markdown) — closes the "
                   "extension-allowlist blind spot, at the cost of a full repo walk.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat path:line symbol | text lines — minimal tokens.")
@click.option("--limit", default=25, show_default=True, help="Max files.")
def find(value: str, regex: bool, case_sensitive: bool, scan_all: bool, as_json: bool,
         terse: bool, limit: int) -> None:
    """Search cached files for VALUE, attributing each hit to its enclosing symbol.

    Grep with the "which method is this in" already filled in — useful for data
    values / string literals (payer names, codes) that `map` can't route on.

    Scans cached (indexed) files only by default — the extension allowlist excludes
    docs/config/markdown, so a literal that also lives in a README or a `.config` is
    invisible here with no signal anything was missed. Pass --all to close that gap:
    it walks the whole repo (respecting .memoignore, same as indexing) and scans
    every additional file too, without adding them to the index.
    """
    root = find_cache_root()
    c = Cache(root)
    entries = c.list_entries()
    if not entries:
        raise click.ClickException(
            "Cache is empty. Run `memo cache-dir <repo> --recursive` first."
        )
    result = find_value(entries, value, regex=regex, ignore_case=not case_sensitive)
    if scan_all:
        extensions = load_extensions(root)
        cached_paths = {normalize_path(Path(e.path)) for e in entries}
        repo_root = root.parent
        spec = load_ignore_spec(repo_root)
        extra_paths = [
            p for p in _walk_sources(repo_root, spec, recursive=True)
            if p.is_file() and CACHE_DIRNAME not in p.parts
            and language_for(p, extensions) is None  # only files the index skips
            and normalize_path(p) not in cached_paths
        ]
        extra_hits = find_value_wide(extra_paths, value, regex=regex,
                                     ignore_case=not case_sensitive)
        result["files"] = sorted(result["files"] + extra_hits,
                                 key=lambda f: (f["is_test"], -len(f["hits"])))
        result["total_hits"] += sum(len(f["hits"]) for f in extra_hits)
        result["scanned"] += len(extra_paths)
        result["wide"] = True
    click.echo(_json_out(result) if as_json else render_find(result, limit=limit, terse=terse))


@main.command()
@click.argument("symbol")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat caller path:line lines — minimal tokens.")
@click.option("--exact", is_flag=True, help="Match the exact symbol name (no fuzzy prefix) — fewer false matches.")
@click.option("--limit", default=40, show_default=True, help="Max caller rows.")
def callers(symbol: str, as_json: bool, terse: bool, exact: bool, limit: int) -> None:
    """Methods that call SYMBOL (candidate, name-based, recall-biased)."""
    _c, g = _load_graph_or_die()
    result = find_callers(g, symbol, exact=exact)
    click.echo(_json_out(result) if as_json else render_callers(result, limit=limit, terse=terse))


@main.command()
@click.argument("symbol")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat callee path:line lines — minimal tokens.")
@click.option("--exact", is_flag=True, help="Match the exact symbol name (no fuzzy prefix).")
@click.option("--limit", default=40, show_default=True, help="Max callee rows.")
def calls(symbol: str, as_json: bool, terse: bool, exact: bool, limit: int) -> None:
    """Project-defined symbols invoked inside SYMBOL's body (candidate, name-based)."""
    _c, g = _load_graph_or_die()
    result = find_calls(g, symbol, exact=exact)
    click.echo(_json_out(result) if as_json else render_calls(result, limit=limit, terse=terse))


@main.command(name="neighbors")
@click.argument("symbol")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--exact", is_flag=True, help="Match the exact symbol name (no fuzzy prefix).")
@click.option("--limit", default=20, show_default=True, help="Max rows per direction.")
def neighbors_cmd(symbol: str, as_json: bool, terse: bool, exact: bool, limit: int) -> None:
    """Both directions at once: who calls SYMBOL and what SYMBOL calls.

    Replaces `memo callers X` + `memo calls X` with one call, for "how does this fit
    into the codebase" — the single most common orientation question.
    """
    _c, g = _load_graph_or_die()
    result = neighbors(g, symbol, exact=exact)
    click.echo(_json_out(result) if as_json
               else render_neighbors(result, limit=limit, terse=terse))


@main.command(name="refs")
@click.argument("field")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--exact", is_flag=True, help="Match the exact field name (no fuzzy prefix).")
@click.option("--limit", default=40, show_default=True, help="Max site rows.")
def refs_cmd(field: str, as_json: bool, terse: bool, exact: bool, limit: int) -> None:
    """Every read/write of FIELD (a C#/VB.NET property) — call edges can't answer this.

    "Who calls this method" and "who reads/writes this field" are different questions:
    a property access has no `(`, so it's structurally invisible to callers/calls/trace.
    Useful for shared mutable state (classic ASP.NET Session/Application, static
    fields) where the bug is "something else changed this out from under me," not
    a bad call. Currently populated only for C#/VB.NET (the only analyzers that
    declare field-like symbols today) — empty for Python/JS/markup is correct, not
    a miss.
    """
    _c, g = _load_graph_or_die()
    result = find_field_refs(g, field, exact=exact)
    click.echo(_json_out(result) if as_json
               else render_field_refs(result, limit=limit, terse=terse))


@main.command(name="trace")
@click.argument("symbol")
@click.option("--up", "up", is_flag=True, help="Walk callers (how execution reaches SYMBOL). Default.")
@click.option("--down", "down", is_flag=True, help="Walk callees (what SYMBOL triggers).")
@click.option("--depth", default=3, show_default=True, help="Levels to walk.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Drop prose header/notes — minimal tokens.")
@click.option("--limit", default=0, show_default=True, help="Max children shown per node (0 = all).")
@click.option("--cross-service", "cross_service", is_flag=True,
              help="Follow declared links (`memo link`) into other registered repos.")
def trace_cmd(symbol: str, up: bool, down: bool, depth: int, as_json: bool, terse: bool,
              limit: int, cross_service: bool) -> None:
    """Walk the call chain from SYMBOL, up (callers) or down (callees)."""
    direction = "down" if down and not up else "up"
    c, g = _load_graph_or_die()
    if cross_service:
        repo_name = services.current_repo_name(c.root.parent)
        result = services.cross_trace(repo_name, g, symbol, direction=direction, depth=depth)
        click.echo(_json_out(result) if as_json
                   else services.render_cross_trace(result, terse=terse, limit=limit))
        return
    result = trace(g, symbol, direction=direction, depth=depth)
    click.echo(_json_out(result) if as_json else render_trace(result, terse=terse, limit=limit))


@main.command(name="guide")
def guide_cmd() -> None:
    """Print the tool-routing guide: when to use memo vs native tools (Glob/Grep/Read).

    A mental model to reason with — not a hardcoded rule table. Read this to
    decide, per need, which tool family fits best.
    """
    click.echo(GUIDE)


@main.command(name="arch")
@click.option("--json", "as_json", is_flag=True, help="Emit raw overview as JSON.")
@click.option("--terse", is_flag=True, help="Compact output.")
@click.option("--limit", default=20, show_default=True, help="Max rows per section.")
def arch_cmd(as_json: bool, terse: bool, limit: int) -> None:
    """Repo overview: languages, symbol counts, top packages, entry points, hotspots."""
    c, g = _load_graph_or_die()
    result = arch(g, c.list_entries())
    click.echo(_json_out(result) if as_json else render_arch(result, limit=limit, terse=terse))


@main.command(name="impact")
@click.argument("symbol")
@click.option("--depth", default=4, show_default=True, help="Max transitive-caller depth.")
@click.option("--exact", is_flag=True, help="Match the exact symbol name (no fuzzy prefix).")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=40, show_default=True, help="Max affected rows per depth.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
def impact_cmd(symbol: str, depth: int, exact: bool, terse: bool, limit: int, as_json: bool) -> None:
    """Blast radius: production methods transitively affected if SYMBOL changes."""
    _c, g = _load_graph_or_die()
    result = impact(g, symbol, max_depth=depth, exact=exact)
    click.echo(_json_out(result) if as_json else render_impact(result, limit=limit, terse=terse))


@main.command(name="trace-stack")
@click.option("--file", "trace_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read the trace from a file instead of stdin.")
@click.option("--terse", is_flag=True, help="One line per frame — minimal tokens.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
def trace_stack_cmd(trace_file: Path | None, terse: bool, as_json: bool) -> None:
    """Resolve a pasted exception trace to current file:line, one call for all frames.

    Reads the trace from --file, or from stdin:

        python -m memo.cli trace-stack --file err.log
        cat err.log | python -m memo.cli trace-stack

    Recognises .NET (including async state machines and lambda closures), JS/TS and
    Python frames. Framework frames are marked so they can be skipped, and any frame
    whose trace line no longer matches the symbol's extent is flagged as build drift —
    a stale line number is the fastest route to a wrong hypothesis.
    """
    text = _read_text(trace_file) if trace_file else click.get_text_stream("stdin").read()
    if not text.strip():
        raise click.ClickException(
            "No trace supplied. Pass --file <path>, or pipe the trace on stdin.")
    _c, g = _load_graph_or_die()
    result = resolve_stack(g, text)
    click.echo(_json_out(result) if as_json
               else render_stack(result, terse=terse))


@main.command(name="tests")
@click.argument("symbol")
@click.option("--depth", default=4, show_default=True,
              help="Max hops from SYMBOL out to a test method.")
@click.option("--exact", is_flag=True, help="Match the exact symbol name (no fuzzy prefix).")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=40, show_default=True, help="Max test rows per depth.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
def tests_cmd(symbol: str, depth: int, exact: bool, terse: bool, limit: int,
              as_json: bool) -> None:
    """Test methods that reach SYMBOL — the minimal set worth running after a change.

    The alternative is running the whole suite, whose failure output is thousands of
    tokens of stack traces, or guessing test files by filename. Returns the reaching
    tests nearest-first plus a ready-to-run filter for the detected framework.

    An empty result is a real answer: nothing reaches SYMBOL in-repo, which is either
    a coverage gap or reflection/DI wiring the name-based graph cannot see.
    """
    _c, g = _load_graph_or_die()
    result = covering_tests(g, symbol, max_depth=depth, exact=exact)
    click.echo(_json_out(result) if as_json
               else render_tests(result, limit=limit, terse=terse))


@main.command(name="changed")
@click.argument("ref")
@click.option("--depth", default=3, show_default=True, help="Max transitive-caller depth.")
@click.option("--terse", is_flag=True, help="Drop prose — minimal tokens.")
@click.option("--limit", default=40, show_default=True, help="Max changed-symbol rows.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
def changed_cmd(ref: str, depth: int, terse: bool, limit: int, as_json: bool) -> None:
    """Map a git diff vs REF to touched symbols and their blast radius (risk)."""
    _c, g = _load_graph_or_die()
    result = changed(g, ref, max_depth=depth)
    click.echo(_json_out(result) if as_json else render_changed(result, limit=limit, terse=terse))


@main.command(name="deadcode")
@click.option("--limit", default=60, show_default=True, help="Max entries to list.")
@click.option("--terse", is_flag=True, help="Drop the caveat prose — minimal tokens.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
def deadcode_cmd(limit: int, terse: bool, as_json: bool) -> None:
    """Production methods with no in-repo caller (heuristic; verify before deleting)."""
    _c, g = _load_graph_or_die()
    result = deadcode(g, limit=limit)
    click.echo(_json_out(result) if as_json else render_deadcode(result, terse=terse))


@main.command(name="routes")
@click.option("--json", "as_json", is_flag=True, help="Emit raw digest as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=0, show_default=True, help="Max rows (0 = all).")
def routes_cmd(as_json: bool, terse: bool, limit: int) -> None:
    """HTTP endpoints declared in this repo, structurally, from the index.

    ASP.NET attribute routing (`[HttpGet]`/`[Route]`), Flask/Express-style
    decorators, and `app.get`/`app.post`/... — first-class and indexed, not a
    query-time regex scan. `memo suggest-links` matches these same declarations
    against another registered repo's outbound calls for cross-service linking.
    """
    _c, g = _load_graph_or_die()
    result = routes_fn(g, limit=limit)
    click.echo(_json_out(result) if as_json else render_routes(result, terse=terse))


@main.command(name="query")
@click.argument("expr")
@click.option("--json", "as_json", is_flag=True, help="Emit raw rows as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=200, show_default=True, help="Max rows returned.")
@click.option("--count", "count_only", is_flag=True,
              help="Print only the match count — for a yes/no question, skips paying for rows.")
def query_cmd(expr: str, as_json: bool, terse: bool, limit: int, count_only: bool) -> None:
    """Ad-hoc filter over the graph: `memo query "symbols kind=method callers>5"`.

    Not Cypher — a flat, ANDed predicate filter over `symbols` or `edges` (call_sites),
    for the questions the fixed commands (callers/calls/impact/...) don't have a shape
    for. Run `memo schema` for the field list and operators.
    """
    _c, g = _load_graph_or_die()
    try:
        result = query_mod.run(g, expr, limit=0 if count_only else limit)
    except query_mod.QueryError as exc:
        raise click.ClickException(str(exc))
    if count_only:
        click.echo(str(result["total"]))
        return
    click.echo(_json_out(result) if as_json else query_mod.render(result, terse=terse))


@main.command(name="schema")
@click.option("--json", "as_json", is_flag=True, help="Emit as JSON.")
def schema_cmd(as_json: bool) -> None:
    """Describe the graph's shape: symbol/edge fields, confidence tiers, edge kinds.

    Read this before writing a `memo query` expression, or to know what a
    `--json` result on any graph command actually contains.
    """
    schema = {
        "symbol_fields": list(graphindex.Graph.SYMBOL_COLS)
                        + ["callers (computed via tiered_indegree — requires resolution "
                           "evidence, not a raw name match; can still under-credit a "
                           "real cross-file Python call, since Python arity isn't "
                           "parsed — verify with `memo callers <name> --exact`)",
                           "pagerank (computed: structural importance over the same "
                           "confidence-filtered edges `callers` uses — high pagerank, "
                           "low callers usually means 'called from few places, but "
                           "those callers are themselves central')"],
        "edge_fields": list(graphindex.Graph.CALL_COLS) + ["verified", "tier (computed)"],
        "field_access_fields": ["field", "path", "line", "enclosing", "enclosing_line",
                                "is_write", "is_test"],
        "confidence_tiers": graphindex.TIER_NAMES,
        "edge_kinds": {
            "static": "name-based candidate edge from source scanning (the default; every edge starts here)",
            "verified": "confirmed by `memo ingest-traces` — outranks every static tier",
            "cross-service": "a manual `memo link` followed by `memo trace --cross-service`, not a graph edge",
            "field-access": "a read or write of a field/property (`memo refs`) — a distinct edge "
                            "type from calls, since property access has no `(`. Populated only "
                            "for kinds an analyzer declares as field-like (today: C#/VB.NET "
                            "`property`); empty for Python/JS/markup is correct, not a gap.",
        },
        "query_ops": list(query_mod.OPS),
    }
    if as_json:
        click.echo(_json_out(schema))
        return
    L = ["# memo graph schema", "",
         "## symbols fields", ", ".join(schema["symbol_fields"]), "",
         "## edges (call_sites) fields", ", ".join(schema["edge_fields"]), "",
         "## field-access fields (`memo refs`)", ", ".join(schema["field_access_fields"]), "",
         "## confidence tiers (low to high)"]
    for val in sorted(schema["confidence_tiers"]):
        L.append(f"- {val}: {schema['confidence_tiers'][val]}")
    L += ["", "## edge kinds"]
    for k, v in schema["edge_kinds"].items():
        L.append(f"- **{k}** — {v}")
    L += ["", "## query operators", ", ".join(schema["query_ops"])]
    click.echo("\n".join(L))


@main.command(name="ingest-traces")
@click.option("--file", "trace_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read JSONL from a file instead of stdin.")
@click.option("--clear", "do_clear", is_flag=True,
              help="Discard all previously ingested runtime edges instead of adding.")
def ingest_traces_cmd(trace_file: Path | None, do_clear: bool) -> None:
    """Merge observed runtime (caller, callee) pairs into the graph as verified edges.

    Upgrades exactly what memo's static scan cannot see — reflection, DI, dynamic
    dispatch — from invisible to `verified`, the top confidence tier. Reads JSONL
    (one `{"caller": ..., "callee": ...}` object per line) from --file or stdin.
    Nothing is re-indexed: ingested edges are merged into the graph on every load and
    can be undone with --clear.
    """
    root = find_cache_root()
    if do_clear:
        removed = traces_mod.clear(root)
        click.echo(click.style(
            "Cleared ingested runtime edges." if removed else "Nothing to clear.",
            fg="green"))
        return
    text = _read_text(trace_file) if trace_file else click.get_text_stream("stdin").read()
    if not text.strip():
        raise click.ClickException(
            "No trace data supplied. Pass --file <path>, or pipe JSONL on stdin.")
    records = traces_mod.parse_jsonl(text)
    if not records:
        raise click.ClickException(
            'No valid records found. Expect one JSON object per line, each with '
            '"caller" and "callee" keys.'
        )
    stats = traces_mod.ingest(root, records)
    msg = (f"Ingested {len(records)} record(s): {stats['added']} new edge(s), "
           f"{stats['updated']} reinforced, {stats['total']} total in {stats['file']}.")

    # Resolve against the current graph right away, rather than leaving the agent to
    # discover silently-dropped pairs later via a separate `query`. Skipped only when
    # there's no cache yet to resolve against (ingest-traces run before any indexing).
    c = Cache(root)
    if c.list_entries():
        g = graphindex.load_or_build(c)
        unresolved = [r for r in records if graphindex._find_symbol(g, r["callee"]) is None]
        resolved_n = len(records) - len(unresolved)
        msg += f" {resolved_n}/{len(records)} callee(s) resolved to a known symbol."
        if unresolved:
            sample = ", ".join(sorted({r["callee"] for r in unresolved})[:5])
            click.echo(click.style(msg, fg="yellow"))
            click.echo(click.style(
                f"{SYM['warn']} {len(unresolved)} callee(s) did not match any indexed "
                f"symbol and were dropped from the graph (still stored raw): {sample}"
                f"{'...' if len(unresolved) > 5 else ''}. Check spelling/qualification, "
                "or re-index if the symbol is new.", fg="yellow"))
            return
    click.echo(click.style(msg, fg="green"))


@main.command(name="run", context_settings={"ignore_unknown_options": True})
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def run_cmd(cmd: tuple[str, ...]) -> None:
    """Run a real command, compressing its stdout for an agent, keeping the original
    recoverable via `memo recall`.

    Everything after `--` is passed through verbatim to the real tool:

        memo run -- pytest -k foo
        memo run -- git diff

    Passes stdout through byte-for-byte, uncompressed, whenever it's a real terminal
    (a human watching) — compression only applies when output is being piped, which is
    the case when an agent's tool-call harness invokes this. Nothing is ever silently
    dropped: a compressed result carries a `memo recall <hash>` pointer to the
    original, and compression only ships at all when it's actually smaller.

    Skips the --terse/--json/--limit flags every other command shares: the output here
    is the wrapped tool's own, not something memo renders.
    """
    import sys

    if not cmd:
        raise click.ClickException("no command given. Usage: memo run -- <cmd> [args...]")
    root = find_cache_root()
    try:
        code = runner_mod.run_command(list(cmd), root)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))
    sys.exit(code)


@main.command(name="recall")
@click.argument("hash_", metavar="HASH")
def recall_cmd(hash_: str) -> None:
    """Print the untruncated original behind a `memo run` compression trailer.

    Every `memo run` output that actually dropped content ends with
    `[full output: memo recall <hash>]` — pass that hash here to get it back.
    """
    root = find_cache_root()
    try:
        text = runner_mod.recall(root, hash_)
    except KeyError:
        raise click.ClickException(
            f"no stored output for hash '{hash_}' — nothing to recall, or "
            "`.memo/recall/` was cleared.")
    click.echo(text, nl=False)


@main.group(name="shim")
def shim_group() -> None:
    """Opt-in PATH shims that auto-route a command through `memo run`.

    Per-command, opt-in only — `memo shim install pytest` shims exactly `pytest`,
    nothing else. This is what makes `memo run`'s compression apply even when the
    caller (a human, an unrelated script, or an agent with no hook API — GitHub
    Copilot's agent mode has none) never types `memo run --` itself. Shims live in
    `~/.memo/shims/`, ahead of the real binary on PATH.
    """


@shim_group.command(name="install")
@click.argument("cmd")
def shim_install_cmd(cmd: str) -> None:
    """Install a PATH shim for CMD, routing it through `memo run`."""
    try:
        real_target = shim_mod.install(cmd)
    except (ValueError, FileNotFoundError) as e:
        raise click.ClickException(str(e))
    click.echo(click.style(
        f"{SYM['ok']} Shimmed '{cmd}' -> {real_target} (via memo run). "
        f"Make sure {shim_mod.SHIM_DIR} is on PATH ahead of the real binary — "
        "`memo doctor` checks this.", fg="green"))


@shim_group.command(name="remove")
@click.argument("cmd")
def shim_remove_cmd(cmd: str) -> None:
    """Remove a previously installed shim for CMD."""
    removed = shim_mod.remove(cmd)
    click.echo(click.style(
        f"Removed shim for '{cmd}'." if removed else f"No shim installed for '{cmd}'.",
        fg="green" if removed else "yellow"))


@shim_group.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Emit raw records as JSON.")
def shim_list_cmd(as_json: bool) -> None:
    """List installed shims."""
    shims = shim_mod.list_()
    if as_json:
        click.echo(_json_out(shims))
        return
    if not shims:
        click.echo("No shims installed. `memo shim install <cmd>` to add one.")
        return
    for cmd, rec in sorted(shims.items()):
        active = shim_mod.is_active(cmd)
        mark = click.style(SYM["ok"], fg="green") if active else click.style(SYM["warn"], fg="yellow")
        status = "active" if active else "installed, but shadowed on PATH"
        click.echo(f"  {mark} {cmd:<12} -> {rec['realpath']}  ({status})")


@main.group(name="adr")
def adr_group() -> None:
    """Architecture Decision Records, linked to symbols and queryable by name.

    The part of "why is this built this way" that static analysis can't recover —
    memo's summaries are structural, never intent. Stored in `.memo/adrs.json`.
    """


@adr_group.command(name="add")
@click.argument("title")
@click.option("--status", default="accepted", show_default=True)
@click.option("--date", default="")
@click.option("--context", default="", help="Why this decision was needed.")
@click.option("--decision", default="", help="What was decided.")
@click.option("--consequences", default="", help="What it costs or enables.")
@click.option("--affects", default="", help="Comma-separated symbol/name patterns this ADR governs.")
@click.option("--json", "as_json", is_flag=True, help="Emit the created record as JSON.")
def adr_add_cmd(title: str, status: str, date: str, context: str, decision: str,
                consequences: str, affects: str, as_json: bool) -> None:
    """Record a new ADR."""
    root = find_cache_root()
    affects_list = [a.strip() for a in affects.split(",") if a.strip()]
    rec = adr_mod.add(root, title, status=status, date=date, context=context,
                      decision=decision, consequences=consequences, affects=affects_list)
    if as_json:
        click.echo(_json_out(rec))
    else:
        click.echo(click.style(f"Recorded ADR-{rec['id']}: {rec['title']}", fg="green"))


@adr_group.command(name="status")
@click.argument("adr_id", type=int)
@click.argument("status")
def adr_status_cmd(adr_id: int, status: str) -> None:
    """Change an ADR's status (e.g. `accepted`, `superseded`, `deprecated`)."""
    if adr_mod.update_status(find_cache_root(), adr_id, status):
        click.echo(click.style(f"ADR-{adr_id} -> {status}", fg="green"))
    else:
        raise click.ClickException(f"No ADR-{adr_id}.")


@adr_group.command(name="delete")
@click.argument("adr_id", type=int)
def adr_delete_cmd(adr_id: int) -> None:
    """Remove an ADR permanently."""
    if adr_mod.delete(find_cache_root(), adr_id):
        click.echo(f"Deleted ADR-{adr_id}.")
    else:
        raise click.ClickException(f"No ADR-{adr_id}.")


@adr_group.command(name="list")
@click.option("--terse", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def adr_list_cmd(terse: bool, as_json: bool) -> None:
    """List all recorded ADRs."""
    records = adr_mod.list_all(find_cache_root())
    click.echo(_json_out(records) if as_json else adr_mod.render_list(records, terse=terse))


@adr_group.command(name="show")
@click.argument("adr_id", type=int)
@click.option("--terse", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def adr_show_cmd(adr_id: int, terse: bool, as_json: bool) -> None:
    """Show one ADR by id."""
    rec = adr_mod.get(find_cache_root(), adr_id)
    if rec is None:
        raise click.ClickException(f"No ADR-{adr_id}.")
    click.echo(_json_out(rec) if as_json else adr_mod.render_one(rec, terse=terse))


@adr_group.command(name="for")
@click.argument("symbol")
@click.option("--terse", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def adr_for_cmd(symbol: str, terse: bool, as_json: bool) -> None:
    """ADRs that affect SYMBOL — the "why was this built this way" lookup."""
    root = find_cache_root()
    records = adr_mod.for_symbol(root, symbol)
    click.echo(_json_out(records) if as_json
               else adr_mod.render_for(symbol, records, terse=terse))


@main.command(name="register")
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--name", default=None, help="Registry name [default: directory name].")
@click.option("--json", "as_json", is_flag=True)
def register_cmd(repo_path: Path, name: str | None, as_json: bool) -> None:
    """Add a repo to the global service registry, for `memo trace --cross-service`."""
    n = services.register(repo_path, name)
    if as_json:
        click.echo(_json_out({"name": n, "path": str(repo_path.resolve())}))
    else:
        click.echo(click.style(f"Registered '{n}' -> {repo_path.resolve()}", fg="green"))


@main.command(name="unregister")
@click.argument("name")
def unregister_cmd(name: str) -> None:
    """Remove a repo from the global service registry.

    Declared links naming this repo are left in place (a link records a fact about
    two repos; removing one side shouldn't silently erase it) but become dangling —
    `memo trace --cross-service` reports them as an unresolvable hop rather than
    failing silently. Clean them up with `memo unlink` if they're genuinely obsolete.
    """
    if not services.unregister(name):
        click.echo(f"'{name}' was not registered.")
        return
    click.echo(f"Unregistered '{name}'.")
    dangling = [i for i, l in enumerate(services.list_links())
                if l["from_repo"] == name or l["to_repo"] == name]
    if dangling:
        click.echo(click.style(
            f"{SYM['warn']} {len(dangling)} link(s) now reference an unregistered repo: "
            + ", ".join(f"[{i}]" for i in dangling)
            + " — see `memo registry`, remove with `memo unlink <index>`.", fg="yellow"))


@main.command(name="registry")
@click.option("--json", "as_json", is_flag=True)
def registry_cmd(as_json: bool) -> None:
    """List registered repos and declared cross-service links."""
    repos, links = services.list_repos(), services.list_links()
    if as_json:
        click.echo(_json_out({"repos": repos, "links": links}))
    else:
        click.echo(services.render_registry(repos, links))


@main.command(name="link")
@click.argument("from_symbol")
@click.argument("to_repo")
@click.argument("to_symbol")
@click.option("--from-repo", "from_repo", default=None,
              help="Registry name for the current repo [default: auto-detected/registered].")
@click.option("--json", "as_json", is_flag=True)
def link_cmd(from_symbol: str, to_repo: str, to_symbol: str, from_repo: str | None,
             as_json: bool) -> None:
    """Declare that FROM_SYMBOL (here) reaches TO_SYMBOL in TO_REPO (a registered repo).

    Manual by design: heuristically guessing HTTP-call boundaries would silently
    misroute as often as it helped, and memo's whole design bias is loud, verifiable
    edges over invented ones. Followed by `memo trace <symbol> --down --cross-service`.
    Find candidates instead of guessing which symbols to name with `memo suggest-links`.
    """
    root = find_cache_root()
    repo = from_repo or services.current_repo_name(root.parent)
    if to_repo not in services.list_repos():
        raise click.ClickException(
            f"'{to_repo}' is not registered. Run `memo register <path> --name {to_repo}` first."
        )
    link = services.add_link(from_symbol, repo, to_symbol, to_repo)
    if as_json:
        click.echo(_json_out(link))
    else:
        click.echo(click.style(
            f"Linked {repo}:{from_symbol} -> {to_repo}:{to_symbol}", fg="green"))


@main.command(name="unlink")
@click.argument("index", type=int)
def unlink_cmd(index: int) -> None:
    """Remove a declared cross-service link by its [index] from `memo registry`."""
    if services.remove_link(index):
        click.echo(f"Removed link [{index}].")
    else:
        click.echo(click.style(f"No link at index {index}. See `memo registry`.", fg="yellow"))


@main.command(name="suggest-links")
@click.option("--json", "as_json", is_flag=True)
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=20, show_default=True)
def suggest_links_cmd(as_json: bool, terse: bool, limit: int) -> None:
    """Propose (never apply) cross-service links: outbound HTTP calls in this repo
    matched against route-like entry points in other registered repos.

    Confirm a candidate with `memo link <from> <to-repo> <to-symbol>` — this only
    ever prints suggestions, in keeping with memo's bias toward links a human
    verified over ones it guessed silently.
    """
    c, g = _load_graph_or_die()
    root_name = services.current_repo_name(c.root.parent)
    result = services.suggest_links(root_name, g)
    if as_json:
        click.echo(_json_out(result))
    else:
        click.echo(services.render_suggestions(result, limit=limit, terse=terse))


@main.command(name="brief")
@click.argument("query")
@click.option("--top", default=8, show_default=True, help="Max methods to include.")
@click.option("--limit", default=0, show_default=True, help="Alias for --top (0 = use --top).")
@click.option("--max-tokens", "max_tokens", default=6000, show_default=True, help="Token budget for the code bundle.")
@click.option("--terse", is_flag=True, help="Omit per-line number prefixes — minimal tokens.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw bundle as JSON.")
def brief_cmd(query: str, top: int, limit: int, max_tokens: int, terse: bool, as_json: bool) -> None:
    """One-shot: the most relevant methods' SOURCE for QUERY, ranked + token-capped.

    Replaces the map->peek->peek... loop: routes, ranks by name-match + call-graph
    centrality + file relevance, and returns the top methods' real code in a single
    bounded call. Best for multi-file investigations (RCA, "how does X work").
    """
    c, g = _load_graph_or_die()
    result = brief(c.list_entries(), query, top=(limit or top), max_tokens=max_tokens, graph=g)
    click.echo(_json_out(result) if as_json else render_brief(result, terse=terse))


@main.command(name="peek")
@click.argument("symbol", required=False)
@click.option("--at", "at_location", default=None, metavar="FILE:LINE",
              help="Look up by exact coordinate (a citation from another memo command) "
                   "instead of by name.")
@click.option("--limit", default=5, show_default=True, help="Max matching symbols to slice.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw slices as JSON.")
@click.option("--terse", is_flag=True, help="Omit per-line number prefixes (header keeps the range).")
def peek_cmd(symbol: str | None, at_location: str | None, limit: int, as_json: bool,
             terse: bool) -> None:
    """Print the exact source slice(s) of SYMBOL, or of a --at FILE:LINE coordinate.

    Replaces reading a whole file: returns just the declaration + body of the
    matching symbol(s), production first. Cheaper than a broad Read while giving
    the real code (not a summary), so RCA accuracy is preserved. Use --at when you
    already have a `file:line` citation from `callers`/`impact`/`find`/`changed` and
    want the code behind it rather than a fresh Read.
    """
    c = _load_cache_or_die()
    if at_location:
        result = peek_at(c.list_entries(), at_location)
        click.echo(_json_out(result) if as_json else render_peek_at(result, terse=terse))
        return
    if not symbol:
        raise click.ClickException("Provide SYMBOL, or --at <file>:<line>.")
    result = peek(c.list_entries(), symbol, limit=limit)
    click.echo(_json_out(result) if as_json else render_peek(result, terse=terse))


@main.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def status_cmd(as_json: bool) -> None:
    """Is the index trustworthy? Freshness of the cache, graph, and rule files.

    memo answers from the last index, so a stale index yields confidently wrong
    coordinates. This surfaces that instead of leaving it to a stderr warning: how
    many indexed files have changed or vanished on disk, whether the graph matches
    the cache, and whether each IDE rule file is present and current.
    """
    root = find_cache_root()
    c = Cache(root)
    index = c.index_snapshot()

    stale, missing = [], []
    for path_str, rec in index.items():
        p = Path(path_str)
        if not p.is_file():
            missing.append(path_str)
        elif sha256_of_file(p) != rec["hash"]:
            stale.append(path_str)

    # Entries built by a different memo can hold outdated symbol data (notably line
    # numbers, which agents are told to trust) even though the file itself is unchanged.
    entries = c.list_entries()
    outdated = [e.path for e in entries if e.memo_version != __version__]

    g = graphindex.load(root)  # already includes merged runtime edges, if any
    # Same check the query commands use, so status and a live warning can never disagree.
    graph_stale_reason = _graph_staleness(g, entries)
    repo = root.parent
    rules = {}
    for key in ALL:
        t = TARGETS[key]
        dest = repo / t.rel_path
        old = dest.read_text(encoding="utf-8") if dest.is_file() else None
        rules[t.rel_path] = render.check(old, t)

    runtime_ingested = len(graphindex._load_runtime_edges(root))
    runtime_verified = sum(1 for cs in g.call_sites if cs.get("verified")) if g else 0
    adr_count = len(adr_mod.list_all(root))
    repo_name = next((n for n, p in services.list_repos().items()
                      if Path(p).resolve() == repo.resolve()), None)
    repo_links = [l for l in services.list_links()
                  if repo_name and repo_name in (l["from_repo"], l["to_repo"])]

    report = {
        "cache_root": str(root),
        "repo_root": str(repo),
        "memo_version": __version__,
        "indexed_files": len(index),
        "stale_files": len(stale),
        "deleted_files": len(missing),
        "outdated_analyzer_files": len(outdated),
        "graph_present": g is not None,
        "graph_symbols": len(g.symbols) if g else 0,
        "graph_edges": len(g.call_sites) if g else 0,
        "graph_built_from_files": g.files if g else 0,
        "graph_matches_cache": bool(g) and graph_stale_reason is None,
        "graph_stale_reason": graph_stale_reason,
        # False for a graph written before fingerprinting: staleness there is only as
        # good as the file count, which misses same-count edits.
        "graph_fingerprinted": bool(g and g.fingerprint),
        "tokens_exact": encoding_available(),
        "rule_files": rules,
        "stale_examples": stale[:10],
        "deleted_examples": missing[:10],
        "runtime_edges_ingested": runtime_ingested,
        "runtime_edges_verified_in_graph": runtime_verified,
        "adr_count": adr_count,
        "registered_as": repo_name,
        "cross_service_links": len(repo_links),
    }
    if as_json:
        click.echo(_json_out(report))
        return

    ok = lambda b: click.style(SYM["ok"], fg="green") if b else click.style(SYM["warn"], fg="yellow")

    click.echo(f"Cache:  {root}")
    click.echo(f"Repo:   {repo}")
    if not index:
        click.echo(click.style(
            f"\n{SYM['warn']} Nothing indexed yet. Run: python -m memo.cli init", fg="yellow"))
        return

    click.echo(f"\nIndex:  {len(index)} file(s)")
    click.echo(f"  {ok(not stale)} {len(stale)} changed on disk since indexing")
    for p in stale[:5]:
        click.echo(f"      {Path(p).name}")
    if len(stale) > 5:
        click.echo(f"      ... and {len(stale) - 5} more")
    click.echo(f"  {ok(not missing)} {len(missing)} indexed file(s) no longer exist")
    if outdated:
        click.echo(f"  {ok(False)} {len(outdated)} indexed by an older memo "
                   f"(now v{__version__}) — symbol line numbers may be wrong")

    if g is None:
        click.echo(click.style(f"\n{SYM['warn']} No code graph — "
                               "callers/calls/trace/impact/arch will not work.", fg="yellow"))
    else:
        click.echo(f"\nGraph:  {len(g.symbols)} symbols, {len(g.call_sites)} call edges")
        if graph_stale_reason:
            click.echo(f"  {ok(False)} {graph_stale_reason}")
        else:
            click.echo(f"  {ok(True)} matches the cache "
                       f"({g.files} file(s), content verified)"
                       if g.fingerprint else
                       f"  {ok(True)} built from {g.files} file(s) vs {len(index)} now "
                       f"cached (count only — re-index to enable content verification)")

    click.echo("\nRule files:")
    marks = {"current": True, "missing": False, "stale": False, "edited": True}
    for path, st in rules.items():
        click.echo(f"  {ok(marks[st])} {st.ljust(8)} {path}")

    click.echo("\nExtensions:")
    click.echo(f"  ADRs: {adr_count}"
               + ("" if adr_count else "  (memo adr add \"<title>\" ...)"))
    if runtime_ingested:
        click.echo(f"  Runtime traces: {runtime_ingested} pair(s) ingested, "
                   f"{runtime_verified} resolved into the graph as verified edges"
                   + ("" if runtime_ingested == runtime_verified else
                      f"  {ok(False)} {runtime_ingested - runtime_verified} did not "
                      "resolve to a known symbol"))
    else:
        click.echo("  Runtime traces: none ingested (memo ingest-traces)")
    if repo_name:
        click.echo(f"  Cross-service: registered as '{repo_name}', "
                   f"{len(repo_links)} declared link(s)")
    else:
        click.echo("  Cross-service: not registered (memo register .)")
    if g:
        n_fields = len({a["field"] for a in g.field_accesses})
        n_routes = len(g.routes)
        click.echo(f"  Field/property refs: {len(g.field_accesses)} site(s) across "
                   f"{n_fields} field(s) (memo refs <field>)"
                   if g.field_accesses else
                   "  Field/property refs: none (C#/VB.NET only; memo refs <field>)")
        click.echo(f"  Routes: {n_routes} endpoint(s) declared (memo routes)"
                   if n_routes else "  Routes: none found (memo routes)")

    if not encoding_available():
        click.echo(click.style(
            f"\n{SYM['warn']} tiktoken unavailable — token counts are ~chars/4 estimates. "
            'Install with: pip install -e ".[tokens]"', fg="yellow"))

    if stale or missing or outdated or not report["graph_matches_cache"]:
        click.echo(click.style(
            "\nIndex is out of date. Refresh:  python -m memo.cli cache-dir . --recursive",
            fg="yellow"))
    elif all(st in ("current", "edited") for st in rules.values()):
        click.echo(click.style(f"\n{SYM['ok']} Index and rule files are current.", fg="green"))
    else:
        click.echo(click.style(
            "\nSome rule files are missing or stale. Refresh:  python -m memo.cli init",
            fg="yellow"))


@main.command(name="doctor")
def doctor_cmd() -> None:
    """Check the environment: Python, dependencies, console encoding, writability."""
    import shutil
    import sys

    checks: list[tuple[bool, str, str]] = []

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append((sys.version_info >= (3, 9), f"Python {pyver}",
                   "memo requires Python 3.9+."))

    for mod, hint in (("click", "pip install click"), ("pathspec", "pip install pathspec")):
        try:
            __import__(mod)
            checks.append((True, f"{mod} importable", ""))
        except ImportError:
            checks.append((False, f"{mod} MISSING", hint))

    checks.append((encoding_available(), "tiktoken (exact token counts)",
                   'Optional. Exact counts: pip install -e ".[tokens]"'))

    # The failure this catches used to be fatal: agents capture stdout through a pipe,
    # where Windows defaults to cp1252 and memo's glyphs raised UnicodeEncodeError.
    enc = getattr(sys.stdout, "encoding", "?")
    safe = console._can_encode(sys.stdout)
    checks.append((safe, f"stdout encoding: {enc}",
                   "Unicode glyphs fall back to ASCII (output is still correct)."))

    checks.append((shutil.which("git") is not None, "git on PATH",
                   "Only `memo changed <ref>` needs git."))

    root = find_cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        writable = False
        checks.append((False, f"cache dir not writable: {root}", str(exc)))
    if writable:
        checks.append((True, f"cache dir writable: {root}", ""))

    if os.environ.get("MEMO_HOME"):
        checks.append((True, f"MEMO_HOME={os.environ['MEMO_HOME']}", ""))

    shims = shim_mod.list_()
    for cmd in sorted(shims):
        active = shim_mod.is_active(cmd)
        checks.append((active, f"shim '{cmd}' on PATH",
                       f"Installed but shadowed by another '{cmd}' earlier on PATH — "
                       f"put {shim_mod.SHIM_DIR} ahead of it, or `memo shim remove {cmd}`."))

    failures = 0
    for good, label, hint in checks:
        if good:
            click.echo(f"  {click.style(SYM['ok'], fg='green')} {label}")
        else:
            failures += 1
            click.echo(f"  {click.style(SYM['warn'], fg='yellow')} {label}")
            if hint:
                click.echo(f"      {hint}")

    click.echo()
    click.echo(click.style(
        f"{SYM['ok']} Environment looks good." if failures == 0
        else f"{failures} item(s) need attention (see hints above).",
        fg="green" if failures == 0 else "yellow"))


@main.command(name="churn")
@click.option("--since", default="1 year ago", show_default=True,
              help="git date range, or 'all' for the full history.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw rows as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
@click.option("--limit", default=30, show_default=True, help="Max rows.")
def churn_cmd(since: str, as_json: bool, terse: bool, limit: int) -> None:
    """Commit frequency, author count, and recency per cached file.

    The one signal memo's index never captured on its own: everything else answers
    "what does the code look like now," this answers "how much has this been
    touched, and by whom." Ranked by a `risk = commits x raw_tokens` proxy — real
    complexity isn't something memo computes; see the command's own output caveat.
    """
    c = _load_cache_or_die()
    result = churn_mod.compute(c.list_entries(), since=since,
                               limit=0 if as_json else limit)
    click.echo(_json_out(result) if as_json else churn_mod.render(result, terse=terse, limit=limit))


@main.command(name="clusters")
@click.option("--json", "as_json", is_flag=True, help="Emit raw clusters as JSON.")
@click.option("--terse", is_flag=True, help="Flat output — minimal tokens.")
def clusters_cmd(as_json: bool, terse: bool) -> None:
    """De-facto module boundaries via Leiden community detection over the call graph.

    Files that call each other heavily cluster together even when the folder layout
    disagrees — the real architectural seams, not just the directory tree. Requires
    `pip install -e ".[graph]"` (python-igraph); without it, prints how to fall back
    to `memo arch`'s directory-based package grouping instead of failing outright.
    """
    _c, g = _load_graph_or_die()
    result = clusters_mod.compute(g)
    if as_json:
        click.echo(_json_out(result))
    else:
        click.echo(clusters_mod.render(result, terse=terse))


@main.command(name="ui")
@click.option("--port", default=8765, show_default=True, help="Preferred port (falls back to a free one if taken).")
@click.option("--no-browser", "no_browser", is_flag=True, help="Print the URL instead of opening it.")
def ui_cmd(port: int, no_browser: bool) -> None:
    """Start a local, read-only dashboard for this repo's index.

    Binds to 127.0.0.1 only — never reachable from the network, never calls out to
    one either. Shows the same data `arch`/`show`/`peek` already expose (raw source
    vs. summary side by side, the call graph, hotspots), as a page instead of text.
    Reads the existing index; run `memo cache-dir . --recursive` first (or after
    changes) the same as for every other command — this does not index anything itself.
    """
    from . import webui
    root = find_cache_root()
    webui.serve(root, port, open_browser=not no_browser)


@main.command()
def show() -> None:
    """List cached files with raw vs. summary token counts and compression ratio."""
    root = find_cache_root()
    c = Cache(root)
    entries = c.list_entries()
    if not entries:
        click.echo("No cached files. Run `memo cache <file>` or `memo cache-dir <dir>`.")
        return

    if not encoding_available():
        click.echo(
            click.style(
                "note: tiktoken unavailable — token counts are a ~chars/4 estimate.",
                fg="yellow",
            ),
            err=True,
        )

    entries.sort(key=lambda e: e.raw_tokens, reverse=True)
    name_w = min(max((len(Path(e.path).name) for e in entries), default=4), 40)
    header = f"{'FILE'.ljust(name_w)}  {'LANG':<11} {'RAW':>8} {'SUMMARY':>8} {'RATIO':>7}"
    click.echo(header)
    click.echo("-" * len(header))
    tr = ts = 0
    for e in entries:
        tr += e.raw_tokens
        ts += e.summary_tokens
        ratio = (e.raw_tokens / e.summary_tokens) if e.summary_tokens else 0
        name = Path(e.path).name
        if len(name) > name_w:
            name = name[: name_w - 1] + "…"
        click.echo(
            f"{name.ljust(name_w)}  {e.language:<11} {e.raw_tokens:>8} "
            f"{e.summary_tokens:>8} {ratio:>6.1f}x"
        )
    click.echo("-" * len(header))
    total_ratio = (tr / ts) if ts else 0
    click.echo(
        f"{'TOTAL'.ljust(name_w)}  {len(entries):<11} {tr:>8} {ts:>8} {total_ratio:>6.1f}x"
    )
    click.echo(f"\nCache: {c.root}")


@main.command()
@click.argument("file_path", required=False, type=click.Path(path_type=Path))
@click.option("--yes", is_flag=True, help="Skip confirmation when clearing everything.")
def clear(file_path: Path | None, yes: bool) -> None:
    """Clear the cache for FILE_PATH, or the entire cache if no path is given."""
    c = Cache(find_cache_root())
    if file_path is not None:
        if c.clear_path(file_path):
            click.echo(f"Cleared cache for {file_path}.")
        else:
            click.echo(f"No cache entry found for {file_path}.")
        return

    if not yes and not click.confirm("Clear the ENTIRE memo cache?"):
        click.echo("Aborted.")
        return
    n = c.clear_all()
    click.echo(f"Cleared entire cache ({n} entr{'y' if n == 1 else 'ies'} removed).")


if __name__ == "__main__":
    main()
