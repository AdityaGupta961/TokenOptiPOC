# memo

**Code intelligence for AI coding assistants. Runs offline, no LLM API, no keys.**

Ask an assistant "who calls `SaveNote`?" and it searches its way through dozens of files.
`memo` answers in one command, with `file:line` coordinates.

Measured with matched A/B pairs — the same assistant, the same question, once with memo and
once on a copy of the repo with memo removed. Across five tasks on two production repos:
**248K tokens → 158K (1.6×), and 84 steps → 40 (2.1×)**, with the same answers. Net of the
assistant's fixed start-up cost, the reduction on the analysis itself is **2.3×**.

The gap is widest where search has no primitive at all — transitive blast radius and call
trees — and narrowest on questions the filesystem already answers, like locating a file
whose name contains the term.

It works by indexing your repo once — per-file structural summaries plus a call graph —
then answering structure and relationship questions from that index instead of by
reading files.

---

## Setup

Three commands. Takes about a minute.

**1. Install memo** (once per machine, from *this* repo):

```bash
cd /path/to/TokenOptimizationPOC
pip install -e ".[tokens]"
```

> The quotes matter — `zsh` and PowerShell both choke on bare `[tokens]`.
> `[tokens]` is optional and only buys exact token counts; if it fails to build,
> `pip install -e .` works fine and memo falls back to estimating.

**2. Set up a project you want to work on** (once per repo):

```bash
cd /path/to/your-project
python -m memo.cli init
```

**3. Restart your IDE** so it picks up the new instruction files.

That's it. `init` builds the index and writes the rule files that tell Claude Code,
Cursor and Copilot when to reach for memo. It is safe to re-run.

### Check it worked

```bash
python -m memo.cli doctor     # environment: Python, deps, encoding, cache writable
python -m memo.cli arch       # your repo: languages, entry points, hotspots
```

If `arch` describes your repo, you're done.

---

## Using it

**You mostly don't.** After `init`, your assistant reaches for memo on its own — that's
what the generated rule files are for. Ask it "who calls this?" or "what breaks if I
change this?" and it will use memo instead of grepping.

To drive it yourself, these are the ones worth knowing:

| Your question | Command |
|---|---|
| Who calls `X`? | `memo callers X` |
| What does `X` call? | `memo calls X` |
| How does execution reach `X`? | `memo trace X --up` |
| What breaks if I change `X`? | `memo impact X` |
| Where is `X` defined and used? | `memo map X` |
| How does this work? (don't know the files yet) | `memo brief "retro priority"` |
| Which method contains this string? | `memo find "PREMERA"` |
| Show me `X`'s actual source | `memo peek X` |
| What is this repo? | `memo arch` |
| What did my branch touch, and what's at risk? | `memo changed main` |

Every one of them accepts:

- `--terse` — coordinates only, ~50% fewer tokens
- `--limit N` — cap the rows
- `--json` — machine-readable

### Looking at it instead of asking it

```bash
python -m memo.cli ui
```

Opens a local dashboard in your browser: raw source next to memo's summary for any
cached file (the same "before → after" this README's numbers come from), the call
graph for the repo's most-connected functions, and the same hotspot/architecture view
as `memo arch`. Read-only, binds to `127.0.0.1` only, and makes no network call of its
own — it renders whatever `.memo/` already has, the same way every other command does.

```bash
python -m memo.cli callers SaveNote --terse
python -m memo.cli trace ProcessCase --up --depth 4
python -m memo.cli brief "kafka consumer" --max-tokens 4000
```

> **`memo` vs `python -m memo.cli`** — examples above are shortened to `memo` for
> readability. `pip` does install a `memo` script, but on Windows it usually lands in a
> `Scripts` directory that isn't on `PATH`. **`python -m memo.cli` always works**, and is
> what the generated rule files use. `python -m memo` works too.

### Keeping the index fresh

The index reflects the last time you ran it. After pulling or a big refactor:

```bash
python -m memo.cli status       # is the index still trustworthy?
python -m memo.cli cache-dir . -r   # re-index (incremental — only changed files)
```

Re-indexing an unchanged 541-file repo takes ~2s, because files whose content hash
already matches are skipped and ignored directories are never walked.

---

## If something looks wrong

| Symptom | Fix |
|---|---|
| `No module named memo` | You're outside the repo and didn't `pip install -e .`. Re-run step 1. |
| Results point at lines that don't match | The index is stale. `python -m memo.cli status`, then `cache-dir . -r`. |
| `(no callers)` for something you know is called | memo can't see reflection, DI or dynamic dispatch. Try the interface's name instead, and check the caller's file type is indexed. See [limits](#honest-limits). |
| `matches N definitions` warning | Several methods share that name. Narrow with `--exact`, or use a qualified name. |
| Assistant ignores memo and greps anyway | Did you restart the IDE? Check `python -m memo.cli status` — it reports rule-file drift. |
| Command fails on Windows with a pipe | Don't pipe memo into `head`/`grep`; they don't exist in PowerShell. Use `--limit N` and `--terse`. |
| Token counts say "approximate" | `tiktoken` isn't installed. `pip install -e ".[tokens]"`, or ignore it. |
| Something else | `python -m memo.cli doctor` |

---

## What `init` actually wrote

| File | For | Loaded |
|---|---|---|
| `AGENTS.md` | the cross-tool standard — Cursor, Codex, Gemini CLI, Zed | always (short trigger list) |
| `.claude/skills/memo/SKILL.md` | Claude Code | on demand — only its description is resident |
| `.cursor/rules/memo.mdc` | Cursor | on demand — "Apply Intelligently" |
| `.github/copilot-instructions.md` | GitHub Copilot | always (short trigger list) |
| `.memoignore` | what to skip when indexing | — |
| `.gitignore` | gains `.memo/` | — |

Only the short trigger list is always in context — 140–420 tokens depending on the tool.
The full routing guide loads only when the assistant decides it's relevant, because a
tool that sells token savings shouldn't spend a four-figure token count per request
advertising itself. Details and per-tool measurements: [integrations/README.md](integrations/README.md).

Write only some of them:

```bash
python -m memo.cli init --agent claude,cursor   # subset
python -m memo.cli init --print                 # preview, write nothing
python -m memo.cli init --dry-run               # report changes, write nothing
python -m memo.cli init --no-index              # rule files only, skip indexing
```

`init` only ever rewrites its own delimited block, so your own instructions in those
files are preserved byte-for-byte. If you edit *inside* memo's block, the next `init`
notices and refuses rather than overwriting you — pass `--force` if you meant it.

---

## Languages

| Language | How it's parsed |
|---|---|
| Python (`.py`) | standard-library `ast` — exact |
| C# (`.cs`) | line + regex heuristics; reads `///` XML doc summaries |
| VB.NET (`.vb`) | line + regex heuristics; reads `'''` XML doc summaries |
| JS / TS / React (`.js .jsx .ts .tsx`) | regex heuristics; React-component aware; reads JSDoc |
| Classic ASP.NET (`.aspx .ascx .ashx .asmx .master .cshtml .vbhtml`) | directive + control heuristics |
| Anything else allowlisted | generic fallback — leading comment and markers only |

VB.NET and classic ASP.NET are the reason memo exists: the tree-sitter ecosystem most
code-intelligence tools build on has no grammar for either, so they can't see those files
at all.

Override the allowlist per project in `.memo/config.json` (this **replaces** the defaults):

```json
{ "extensions": [".cs", ".vb", ".ts", ".tsx", ".razor"] }
```

---

## Where the cache lives

```
.memo/
  index.json            # file path -> latest content hash
  cache/<sha256>.json   # one summary per unique file content
  manifest.json         # all entries in one file, so a query is one read not N
  graph.json            # symbols + call edges (columnar, paths interned)
  shards.json           # per-content-hash scan results, for incremental re-indexing
```

`manifest.json`, `graph.json` and `shards.json` are derived — safe to delete, rebuilt on
the next index, and validated against both the index and the memo version so a stale one
is never served.

memo finds `.memo/` by walking up from the current directory, so running it in a
subfolder uses the project's cache. Set `MEMO_HOME=/path/to/project` to force one.

---

## Honest limits

**The call graph is name-based.** An edge means a method's body contains `name(`. So it
is recall-biased on purpose: it includes interface/implementation fan-out and same-named
methods across classes, and it cannot see reflection, dynamic dispatch or aliased calls.
Every result carries `file:line` so one glance confirms it.

Where it can, memo scores each edge and sorts the confident ones first:

| Marker | Meaning |
|---|---|
| `verified` | confirmed by real execution via `memo ingest-traces` — not inferred, outranks everything |
| `exact` | the receiver's *declared* type matches — `ICaseProcessor _p;` then `_p.Save(x)` |
| `strong` | same file with a fitting argument count, or the caller's own class |
| unmarked | no evidence either way — the common case |
| `!` / `contra` | the argument count can't reach this definition |

**No edge is ever hidden by a marker** — it changes the order, never the membership. A
`!` row may still be the call you want, since VB paren-less calls, C# extension methods
and generic calls all yield incomplete counts.

**The savings depend on the host actually loading the rule files.** A live Claude Code,
Cursor or Copilot session gets `AGENTS.md`/the skill/the `.mdc` primed automatically —
that's the whole mechanism. A subagent, a different harness, or any orchestration layer
that doesn't replicate that priming gets none of it: measured directly, an agent given a
fully-indexed repo but no nudge toward its onboarding file did not discover memo at all
and produced the identical token cost (56.7K) as one explicitly forbidden from using it
(54.8K) — memo sitting unused is indistinguishable from memo absent. Once discovered
(pointed at `AGENTS.md`) the same question dropped to 49.7K — real, but only an 11%
saving, because the agent explored with 12 individual `peek` calls instead of leading
with one `brief`. Doing that same investigation the way the guide actually prescribes
(`brief` + one follow-up) measured 3.4K tokens — a 94% reduction. The gap between 11%
and 94% is not memo's ceiling changing; it's how faithfully the caller follows the guide,
and that is the single biggest lever on the numbers in this README, bigger than any
single command's design.

**It won't explain logic.** Summaries are structural, from parsing, not from a model.
They tell you *where* to look and *what* is there — not why the code is written that way.
Nothing is invented: if the parser can't determine something, it's omitted.

**It helps during discovery and planning, not editing.** Agentic edit modes re-read files
from disk before changing them, which is correct — an editor needs exact current
contents, not a summary. Treat memo as a planning-time token optimizer.

**memo doesn't shrink small files.** A 60-token file summarizes to more than it started
with. The wins are on large files and on cross-file questions, which is where the tokens
actually are.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q     # 368 tests
```

`memo/guide.py` is the single source for everything `init` writes. Don't hand-edit the
generated files — change the guide and re-run `init`.

## License

MIT.
