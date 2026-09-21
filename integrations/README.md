# Instructing agents to use `memo`

**These files are generated. Don't hand-write them.**

```bash
cd <your-repo>
python -m memo.cli init
```

That writes a rule file for each supported assistant, adds `.memo/` to `.gitignore`,
seeds a `.memoignore`, and builds the index. It is safe to re-run.

| Assistant | File written | Body | Loaded |
|---|---|---|---|
| **AGENTS.md** (cross-tool standard) | `AGENTS.md` | stub | always — also read by Cursor, Codex, Gemini CLI, Zed |
| **Claude Code** | `.claude/skills/memo/SKILL.md` | full | on demand — only the skill's `description` stays resident |
| **Cursor** | `.cursor/rules/memo.mdc` | full | on demand — `alwaysApply: false` + description ("Apply Intelligently") |
| **GitHub Copilot** (VS Code) | `.github/copilot-instructions.md` | stub | always — needs no setting, unlike AGENTS.md there |

## Why two bodies

memo sells token savings, so guidance a host agent loads *unconditionally* has to be
cheap. Everything always-on gets the **stub**: the trigger list, the seven commands worth
knowing, and the one rule whose violation cancels the saving. The **full guide** — trust
boundaries, confidence tiers, platform notes — goes only where loading is deferred until
the agent asks for it, or is available on request via `memo guide`.

Measured on this repo, per request:

| Tool | Before | After |
|---|---|---|
| Claude Code | 1,184 | **140** |
| Cursor | 2,353 | **421** |
| GitHub Copilot | 1,184 | **340** |

Cursor's figure was double because Cursor reads `AGENTS.md` *as well as* `.cursor/rules`,
and the `.mdc` was `alwaysApply: true` — the same manual, twice, on every request.

Pick a subset with `--agent`:

```bash
python -m memo.cli init --agent cursor,copilot
python -m memo.cli init --print          # preview, write nothing
python -m memo.cli init --dry-run        # report changes, write nothing
```

## Why generated, not checked in

This directory used to hold three hand-maintained copies of the routing guidance, with
a note warning that all three had to be edited in lockstep. They drifted. Now there is
exactly one source — `memo/guide.py` — and `memo init` renders it per target, filling in
each assistant's own tool names (Claude Code hears "Glob, Grep, Read"; Copilot hears
"file search and read"). `memo guide` prints the tool-neutral version.

The Claude Code skill proved the point a second time: it was hand-written and outside
`init`, and within one version it had fallen a section behind the guide while still
carrying a grammar artifact the template had already fixed. It is generated now.

## Safe to re-run

Generated content lives inside a delimited block:

```
<!-- BEGIN memo v0.3.0 sha=... -->
...
<!-- END memo -->
```

`init` only ever rewrites that block, so your own instructions in `AGENTS.md` or
`copilot-instructions.md` are preserved byte-for-byte. The `sha=` records what memo last
wrote: if you edit *inside* the block, the next `init` detects it, skips the file, and
tells you to pass `--force` if you really want it replaced.

`.cursor/rules/memo.mdc` and `.claude/skills/memo/SKILL.md` are the exceptions — memo owns
those files outright, which keeps the YAML frontmatter on line 1 as both formats require.
Their `description` frontmatter is the load-bearing part: it is the only text the agent
sees while deciding whether to pull the body in, so it names trigger conditions rather
than summarising.

## Prerequisites

```bash
pip install -e <path-to-this-repo>          # once per machine
pip install -e "<path-to-this-repo>[tokens]"  # optional: exact token counts via tiktoken
```

Invoke as `python -m memo.cli` (or `python -m memo`). There is a `memo` console script,
but pip installs it into a user-scheme `Scripts` directory that is often not on `PATH`
on Windows — so nothing here depends on it.

Re-index after substantial code changes; `python -m memo.cli status` tells you when the
index has gone stale.
