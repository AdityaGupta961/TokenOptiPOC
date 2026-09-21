"""The single canonical source of memo's agent-facing instructions.

`memo guide` prints it, and `memo init` renders it into each IDE's rule-file format
(AGENTS.md, .claude/skills/memo/SKILL.md, .cursor/rules/memo.mdc,
.github/copilot-instructions.md). Generating all four from here makes drift between them
impossible — see integrations/README.md for the two occasions drift happened anyway,
both times to a file that was maintained by hand instead.

There are two bodies, not one. `GUIDE_TEMPLATE` is the full guide, for surfaces a host
agent loads only when it decides the topic is relevant. `STUB_TEMPLATE` is the short
always-on form. Which one a target gets is decided in targets.py by *when the host loads
the file*, never by how much there is to say.

Written against vendor guidance for instruction files, which is unusually specific:

  * Anthropic documents a 200-line target for CLAUDE.md because "longer files consume
    more context and reduce adherence", and warns that vague or conflicting instructions
    get picked arbitrarily. So this is short, imperative and free of hedging.
  * Cursor lists "exhaustive command references" as an anti-pattern, so only the
    commands an agent will actually reach for are listed — not the full CLI surface.
  * Both warn that content derivable from the codebase is noise. What survives here is
    what an agent cannot infer: that memo exists, when it beats reading files, and how
    far its answers can be trusted.
  * Claude Code's `@path` imports do NOT reduce context (imported files load at launch),
    so brevity here is the only thing that lowers the per-request cost.

Host agents name their built-in tools differently, so those names are placeholders.
Every placeholder is substituted as a NOUN PHRASE and only ever appears in a
noun-phrase slot — an earlier version dropped verb-shaped values into noun slots and
produced text like "Native a files-with-matches search is tiny", which is exactly the
kind of garbled instruction that erodes compliance.

Substitution is plain string replacement rather than str.format/Template because the
text legitimately contains braces and `$`, which those would misparse.
"""

from __future__ import annotations

# Placeholder tokens replaced by memo.render using each Target's Native names.
TOKENS = ("{{native_list}}", "{{read}}", "{{search}}", "{{glob}}")

GUIDE_TEMPLATE = """\
# `memo` — code intelligence for this repo

This repo has a prebuilt `memo` index in `.memo/`: per-file summaries plus a call
graph. `memo` answers structure and relationship questions without reading whole
files. Invoke it as `python -m memo.cli <cmd>` from anywhere in the repo.

## Reach for memo when the question spans files or is about relationships

| Need | Command |
|---|---|
| Who calls X, what X calls, how execution reaches X | `memo callers X` / `memo calls X` / `memo trace X --up` |
| What breaks if X changes | `memo impact X`, or `memo changed <git-ref>` for a diff |
| Which tests reach X, to verify a change without the whole suite | `memo tests X` — prints a ready-to-run filter |
| You have a stack trace or exception log | `memo trace-stack --file <log>` (or pipe it in) — resolves every frame at once, marks framework frames, flags stale line numbers |
| How does X work, and you don't yet know which file/symbol answers it | `memo brief "<terms>"` — returns ranked method source, several at once |
| How does X work, and X is a name you already have exactly | `memo peek X` — one call: signature, line, source |
| Which method contains a literal value | `memo find "<value>"` |
| Where X is defined and referenced | `memo map X` |
| What this repo is, where requests enter | `memo arch` |
| A question the fixed commands don't fit (e.g. "all methods in package X with >5 callers") | `memo query "symbols kind=method callers>5 path~X"` — run `memo schema` first for fields |
| Why a symbol reached its callers/dispatch even though it's not in the source | check `memo ingest-traces` has been run for it — verified edges outrank name-based ones |
| Why something is built this way, not just what it does | `memo adr for X` |
| A call chain that crosses into another repo/service | `memo trace X --down --cross-service` (only works for repos + boundaries already declared via `memo register`/`memo link`; `memo suggest-links` finds candidate boundaries to confirm) |
| Full picture of how X fits in (who calls it AND what it calls) | `memo neighbors X` — one call instead of `callers` + `calls` |
| Just the source at a coordinate you already have | `memo peek --at <file>:<line>` |
| Who reads/writes a shared field or property (not a method call — no `(`, so callers/calls/trace can't see it) | `memo refs FieldName` — C#/VB.NET `property` declarations only |
| Which files are risky to touch (churn, not just size or hotspot count) | `memo churn` — commits x tokens, ranked; needs a git repo |
| What HTTP endpoints this repo exposes | `memo routes` — structural, from the index, not a live scan |
| A literal that might live in a doc/config file, not just source (`find` returned nothing but you suspect it exists) | `memo find "<value>" --all` — scans non-cached files too |
| Running a real command (tests, lint, build, git) whose output would be mostly noise | `memo run -- <cmd> [args]` — compresses stdout, prints `memo recall <hash>` if anything was cut, never loses data |

For `brief`, `map` and `find`, pick rare domain terms (`IsRetroCase`, `retro`). Drop
generic ones (`data`, `service`, `get`) — they match everything and rank nothing.
`brief` ranks by name match, file relevance, real call-graph PageRank, and (with the
optional `[search]` extra installed) BM25 over each candidate's doc comment too — so a
query matching only a docstring, not any name, can still surface the right method.
`memo schema` lists every computed field `query` can filter/sort on, including `pagerank`.

## Reach for your own tools when the target is a single known thing

- A file you can already name — {{read}}, once. One cohesive read beats several `peek`s.
- A filename pattern — {{glob}}.
- A rare literal string, or one definition you can already pin down — {{search}}.

## Rules

1. **Cite memo's `file:line` as given. Do not re-read the file to confirm it.**
   Re-verifying memo's output with your own reads is the largest single waste; it
   cancels out the saving. When you need to actually see the code behind a citation
   — not just cite it — that is `memo peek --at <file>:<line>` (or `memo peek <name>`
   if you have the name instead), never {{read}}: peek returns exactly that span,
   {{read}} returns the whole file. Read further with your own tools only for
   content memo did not return at all, such as a branch body or a config value.
2. Lead with the one command that fits. Take at most three follow-up calls, then
   answer. Do not explore by firing many small commands. In particular: if the
   question needs the source of *several* functions to answer (tracing a chain,
   understanding a subsystem), that is `brief`, in one call — not `peek` on each
   function by name one at a time. A/B measured: an agent that already knew this rule
   still called `peek` 8 times instead of `brief` once, at 15x the token cost for the
   same answer (49,651 vs. 3,397 tokens). If you find yourself about to call `peek`
   more than twice in the same investigation, stop and use `brief` instead.
3. Add `--terse` for minimal output, `--limit N` to cap rows, `--json` to parse. Every
   query command accepts all three.
4. If a command fails, returns nothing, or reports an empty cache, fall back to
   {{native_list}} and carry on. Do not retry, and do not try to rebuild the index.
5. Never run `memo init` or `memo clear`. `init` overwrites these instruction files;
   `clear` destroys the index.
6. `memo adr add`, `memo ingest-traces`, `memo link`, `memo register` and
   `memo shim install`/`memo shim remove` write persistent state a teammate (or the
   next session on this machine) will later see (an ADR, a runtime edge, a
   cross-repo link, a PATH shim) — the same category as rule 5, not a query. Only
   run one because the user asked for that specifically, never speculatively while
   just investigating. `memo run -- <cmd>` is not in this category — it's the normal
   way to execute a command, not a side-effecting state write.

## How far to trust memo's answers

The call graph is **name-based**: an edge means the caller's body contains `name(`.
Edges are therefore *candidates*, not verified calls — same-named methods in different
classes get conflated, and reflection, dependency injection and dynamic dispatch are
invisible. That is precisely why rule 1 is safe rather than reckless: a wrong edge is
obvious from the line memo cites, so glance at that line instead of re-reading the
file. When memo warns `matches N definitions`, narrow with `--exact` or a qualified
name.

Rows carry a confidence marker where there is evidence either way. **No edge is ever
hidden by it** — a marker changes the order of results, never their membership.

**Rows arrive sorted by confidence, best first.** That ordering is the signal — in
`--terse` there is no marker on confident rows, because a marker would cost a token per
row to repeat what the sort already says.

| Marker | Meaning |
|---|---|
| `(verified)` | Confirmed by real execution via `memo ingest-traces` — not inferred, outranks every other tier |
| `(exact)` | The receiver's **declared type** matches this definition's type, or implements it — `ICaseProcessor _p;` then `_p.Save(x)` resolves to `CaseProcessor.Save` |
| `(strong)` | Same file with a fitting argument count, or the caller's own class |
| unmarked | No evidence either way; the common case |
| `!` or `(contra)` | The argument count cannot reach this definition |

`!` is the only marker `--terse` prints, because it is the one thing the ordering cannot
express. Glance at such a row before relying on it — but it is still a real `name(`
occurrence and may well be the call you want, since VB paren-less calls, C# extension
methods and generic calls all yield incomplete counts. Never report a `!` edge as absent.

An `(exact)` row is the one case worth trusting over your own guess: the receiver's type
was read from a declaration, not inferred from naming. Declared types are file-scoped, so
a field on the other half of a `partial class` is invisible, and a name declared as two
different types is reported as neither.

Symbols come from the last index; reference counts are read live from disk. Run
`memo status` if results look wrong — it reports whether the index has gone stale, and
if it has, say so rather than presenting stale coordinates as fact.

## On Windows and PowerShell

Do not pipe memo into `head`, `tail`, `grep` or `wc`; they do not exist there and the
command fails. Use `--terse` and `--limit N` instead.

Run **one** memo command per invocation, with nothing before or after it. `&&`, `||` and
`;` are bash operators — PowerShell rejects them as statement separators and the whole
call fails, which reads like "memo is broken" when memo was never reached. If that
happens, re-run the command on its own rather than debugging the chain.
"""

# Tool-neutral phrasing, for `memo guide` where no host agent is known.
_NEUTRAL = {
    "{{native_list}}": "your own search and read tools",
    "{{read}}": "read it directly",
    "{{search}}": "your text search",
    "{{glob}}": "your filename search",
}


# The always-on half. Every surface that a host agent loads unconditionally gets THIS,
# not the guide above — because a tool whose whole claim is saving tokens cannot justify
# spending a four-figure token count per request advertising itself. The full guide moves
# to surfaces that load on demand: a Claude Code Skill, a Cursor "Apply Intelligently"
# rule, or `memo guide` on request.
#
# What earns its place here is only what an agent cannot recover later: that memo exists,
# the question shapes it beats reading files for, the exact command for each, and the one
# rule whose violation cancels the entire saving. The command column is deliberately kept
# — without it the agent knows memo exists but not how to call it, and would have to spend
# a tool call on `memo guide` before every use.
#
# `tests` earned its row here rather than staying full-guide-only after a live A/B trial:
# an agent asked "which tests cover this function" read this stub, never called `memo
# guide` (nothing told it the stub was incomplete), and had no idea `memo tests` existed —
# it spent 28 tool calls and ~64,700 tokens re-deriving by hand what `memo tests <symbol>`
# answers in one call at 845 tokens. The stub cannot rely on an agent proactively fetching
# the full guide before it discovers it needs to; a need this common (checking blast radius
# on tests before a change) has to be discoverable from the stub alone, the same as `impact`.
#
# `changed` was added on the same reasoning, found by auditing the rest of the command
# surface against the same failure mode rather than waiting for another live incident to
# surface it: "what did my branch touch, what's at risk" is at least as common a real
# question as anything else in this table (every pre-commit/pre-review check starts here),
# and it was previously invisible from the stub entirely — an agent would fall back to raw
# `git diff` plus per-symbol impact checks, one at a time, exactly the shape of waste `tests`
# was already measured causing.
STUB_TEMPLATE = """\
# `memo` — code intelligence for this repo

This repo has a prebuilt `memo` index in `.memo/` — use it, not {{native_list}}, for any
question spanning files or relationships. Reach for {{native_list}} only for a single,
already-named thing.

| Need | Command |
|---|---|
| Who calls X, what X calls, how execution reaches X | `memo callers X` / `memo calls X` / `memo trace X --up` |
| What breaks if X changes | `memo impact X` |
| What did my branch/diff touch, and what's at risk | `memo changed <git-ref>` |
| What to test after changing X, before running the whole suite | `memo tests X` |
| You have a stack trace | `memo trace-stack --file <log>` |
| How does X work — name not known yet | `memo brief "<terms>"` |
| How does X work — you have the exact name | `memo peek X` |
| Code behind a `file:line` citation you already have | `memo peek --at <file>:<line>` |
| Where X is defined and referenced | `memo map X` |
| Which method contains a literal value | `memo find "<value>"` |
| What this repo is, where requests enter | `memo arch` |
| Running a command whose output is mostly noise (tests, lint, build, git) | `memo run -- <cmd> [args]` |

Invoke as `python -m memo.cli <cmd>`. Add `--terse` for minimal output, `--limit N` to
cap rows, `--json` to parse.

**Cite memo's `file:line` as given; use `peek --at` for the code, never a raw read
to confirm it** — that re-read is the largest single waste and cancels the saving.

Full routing rules, trust boundaries and platform notes: `python -m memo.cli guide`.
"""


def _substitute(template: str, names: dict[str, str] | None) -> str:
    out = template
    for token, value in (names or _NEUTRAL).items():
        out = out.replace(token, value)
    return out


def render_guide(names: dict[str, str] | None = None) -> str:
    """Substitute native-tool placeholders. Defaults to tool-neutral phrasing."""
    return _substitute(GUIDE_TEMPLATE, names)


def render_stub(names: dict[str, str] | None = None) -> str:
    """The short always-on form, for surfaces a host agent loads unconditionally."""
    return _substitute(STUB_TEMPLATE, names)


# Backwards-compatible module attribute for `memo guide`.
GUIDE = render_guide()
