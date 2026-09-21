"""Managed-block rendering and upsert.

This is the highest-risk code in memo: `init` writes into files that already contain
the user's own instructions. A bug here silently destroys someone's CLAUDE.md, and
they may not notice for weeks. Everything below is about proving we only ever touch
the bytes between our own markers.
"""

from __future__ import annotations

import pytest

from memo import render
from memo.targets import ALL, TARGETS


# --- upsert: the idempotency and safety contract -----------------------------

def test_creates_block_in_empty_file():
    out, status = render.upsert(None, "BODY")
    assert status == "created"
    assert "BODY" in out
    assert render.END in out


@pytest.mark.parametrize("existing", ["", "   ", "\n\n"])
def test_blank_file_treated_as_empty(existing):
    out, status = render.upsert(existing, "BODY")
    assert status == "created"
    assert out.strip().endswith(render.END)


def test_appends_without_disturbing_existing_content():
    existing = "# My Project\n\nAlways run `dotnet test`.\nUse tabs.\n"
    out, status = render.upsert(existing, "BODY")
    assert status == "created"
    # Every original byte survives, in order, at the front.
    assert out.startswith(existing)
    assert "BODY" in out


def test_rerun_is_byte_identical():
    first, _ = render.upsert("# Mine\n", "BODY")
    second, status = render.upsert(first, "BODY")
    assert status == "unchanged"
    assert second == first, "re-running init must not perturb the file at all"


def test_updates_in_place_preserving_surroundings():
    """The block sits BETWEEN user content; updating it must not move or eat either side."""
    before = "# Header\n\nkeep me above\n\n"
    after = "\n## Footer\n\nkeep me below\n"
    v1 = before + render.wrap("OLD BODY") + after

    v2, status = render.upsert(v1, "NEW BODY")
    assert status == "updated"
    assert "NEW BODY" in v2
    assert "OLD BODY" not in v2
    # Both sides intact, and the block stayed in the middle rather than relocating.
    assert v2.startswith(before)
    assert v2.endswith(after)


def test_hand_edit_inside_block_is_refused():
    v1, _ = render.upsert(None, "GENERATED BODY")
    tampered = v1.replace("GENERATED BODY", "I edited this by hand")
    out, status = render.upsert(tampered, "NEW BODY")
    assert status == "conflict"
    assert out == tampered, "a conflict must leave the file completely untouched"
    assert "I edited this by hand" in out


def test_force_overrides_hand_edit():
    v1, _ = render.upsert(None, "GENERATED BODY")
    tampered = v1.replace("GENERATED BODY", "I edited this by hand")
    out, status = render.upsert(tampered, "NEW BODY", force=True)
    assert status == "updated"
    assert "I edited this by hand" not in out
    assert "NEW BODY" in out


def test_unterminated_marker_is_not_treated_as_a_block():
    """A truncated file must not cause us to guess an extent and eat the remainder."""
    broken = "# Mine\n<!-- BEGIN memo v0.0.0 sha=deadbeef -->\nhalf a block\n"
    out, status = render.upsert(broken, "BODY")
    assert status == "created"
    assert out.startswith(broken)
    assert "half a block" in out


def test_content_outside_block_never_changes_across_many_rounds():
    text = "TOP\n"
    for i in range(5):
        text, _ = render.upsert(text, f"BODY {i}")
        text += "" if text.endswith("\n") else "\n"
    assert text.startswith("TOP\n")
    assert "BODY 4" in text
    # Exactly one managed block, no accumulation.
    assert text.count(render.END) == 1


# --- rendering --------------------------------------------------------------

def test_wrap_embeds_version_and_hash():
    block = render.wrap("BODY")
    assert "BEGIN memo v" in block
    assert "sha=" in block
    assert block.rstrip().endswith(render.END)


@pytest.mark.parametrize("key", ALL)
def test_every_target_renders_non_trivially(key):
    body = render.render_body(TARGETS[key])
    assert len(body) > 400, "a rule file that short is almost certainly broken"
    assert "memo" in body
    # Placeholders must all be substituted — a leaked {{token}} would ship to users.
    assert "{{" not in body and "}}" not in body


# --- always-on versus on-demand ---------------------------------------------
#
# The split is the whole point of the target table: memo sells token savings, so a body
# that a host agent loads unconditionally has to be short. These tests pin which surface
# gets which body, because getting it backwards is invisible in output and expensive.

ALWAYS_ON = ("agents", "copilot")
ON_DEMAND = ("claude", "cursor")


@pytest.mark.parametrize("key", ALWAYS_ON)
def test_unconditionally_loaded_surfaces_get_the_short_stub(key):
    assert TARGETS[key].style == "stub"
    body = render.render_body(TARGETS[key])
    assert len(body) < 1800, "this file loads on every request; it must stay small"
    # It still has to be self-sufficient: name the tool, the triggers and the commands.
    assert "memo callers" in body and "memo impact" in body
    assert "memo.cli guide" in body, "must say where the full rules are"


@pytest.mark.parametrize("key", ON_DEMAND)
def test_deferred_surfaces_get_the_full_guide(key):
    assert TARGETS[key].style == "full"
    body = render.render_body(TARGETS[key])
    assert "How far to trust memo's answers" in body
    assert "fall back to" in body


def test_the_stub_is_much_cheaper_than_the_full_guide():
    stub = render.render_body(TARGETS["agents"])
    full = render.render_body(TARGETS["cursor"])
    assert len(stub) < len(full) / 2, "the stub exists to be a fraction of the guide"


def test_claude_target_is_a_skill_not_an_always_loaded_memory_file():
    """A CLAUDE.md holding this guide is pure always-on cost — imports don't defer it.

    A skill's body loads only on invocation, so the description carries the triggers.
    """
    t = TARGETS["claude"]
    assert t.rel_path == ".claude/skills/memo/SKILL.md"
    assert t.exclusive, "a skill needs its frontmatter on line 1"
    text = render.file_text(t)
    assert text.startswith("---\n")
    assert "name: memo" in text
    assert "description:" in text


def test_the_skill_description_says_when_not_to_load():
    """Without a negative case the skill fires on single-file reads, where Read is cheaper."""
    text = render.file_text(TARGETS["claude"])
    head = text.split("---", 2)[1]
    assert "Not for" in head


def test_claude_body_names_claudes_own_tools():
    body = render.render_body(TARGETS["claude"])
    assert "Glob, Grep and Read" in body


@pytest.mark.parametrize("key", ALL)
def test_no_target_tells_the_agent_to_run_init(key):
    """Regression: every generated file used to open with "Index this repo once:
    python -m memo.cli init" — but the agent reads these files *because* init already
    ran, and following that would overwrite the file it was reading."""
    body = render.render_body(TARGETS[key])
    assert "memo.cli init" not in body.replace("`python -m memo.cli init`", "")
    assert "## Setup" not in body


@pytest.mark.parametrize("key", ALL)
def test_substituted_text_is_grammatical(key):
    """Regression: verb-shaped native-tool values were dropped into noun-phrase slots,
    shipping sentences like "Native a files-with-matches search is tiny"."""
    body = render.render_body(TARGETS[key])
    for bad in ("Native a ", "-> a filename", ". read that file",
                "search for filename patterns"):
        assert bad not in body, f"{key}: ungrammatical fragment {bad!r}"


def test_full_guide_states_the_fallback_rule():
    """Documented as the reliably-followed form: a concrete conditional."""
    body = render.render_body(TARGETS["cursor"])
    assert "fall back to" in body
    assert "empty cache" in body


def test_cursor_rule_is_apply_intelligently_not_always_on():
    """Regression: `alwaysApply: true` duplicated AGENTS.md on every Cursor request.

    Cursor reads AGENTS.md *as well as* .cursor/rules, so an always-on rule carrying the
    same guidance cost roughly 2,350 tokens per request for one manual. `alwaysApply:
    false` plus a description is Cursor's "Apply Intelligently" type, which loads the body
    only when the agent judges it relevant — so the description has to carry the triggers.
    """
    text = render.file_text(TARGETS["cursor"])
    assert text.startswith("---\n"), "Cursor requires YAML frontmatter on line 1"
    assert "alwaysApply: false" in text
    assert "alwaysApply: true" not in text
    head = text.split("---", 2)[1]
    assert "description:" in head
    for trigger in ("who calls", "breaks if", "architecture"):
        assert trigger in head, f"description must name the trigger {trigger!r}"


def test_cursor_file_has_no_managed_markers():
    """memo owns the .mdc outright; markers there would only risk the frontmatter."""
    text = render.file_text(TARGETS["cursor"])
    assert "BEGIN memo" not in text and render.END not in text


# --- check(): must agree with what init would do ----------------------------

@pytest.mark.parametrize("key", ALL)
def test_check_reports_missing_then_current(key):
    t = TARGETS[key]
    assert render.check(None, t) == "missing"
    text = render.file_text(t) if t.exclusive else render.upsert(None, render.render_body(t))[0]
    assert render.check(text, t) == "current"


def test_check_detects_stale_and_edited():
    t = TARGETS["copilot"]
    # An older memo wrote a different body: safe to refresh.
    old, _ = render.upsert(None, "ANCIENT BODY")
    assert render.check(old, t) == "stale"
    # A human changed bytes *inside* the block: must be reported as edited, not stale,
    # so init refuses instead of silently overwriting their work.
    current, _ = render.upsert(None, render.render_body(t))
    tampered = current.replace("| Need | Command |", "| Need | Command | (ours) |")
    assert tampered != current, "fixture must actually modify the body"
    assert render.check(tampered, t) == "edited"
