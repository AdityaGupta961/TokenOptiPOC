"""`memo.filters` — per-tool stdout compressors used by `memo run`."""

from __future__ import annotations

from pathlib import Path

from memo.filters import (
    compress, normalize_tool_name, _compress_generic, _compress_pytest,
    _compress_pass_fail_lines, _compress_cargo_test, _load_max_lines,
)

# --- normalize_tool_name ----------------------------------------------------

def test_normalize_tool_name_pytest():
    assert normalize_tool_name(["pytest", "-k", "foo"]) == "pytest"
    assert normalize_tool_name(["py.test"]) == "pytest"


def test_normalize_tool_name_npm_yarn_test():
    assert normalize_tool_name(["npm", "test"]) == "npm_test"
    assert normalize_tool_name(["yarn", "test"]) == "npm_test"
    assert normalize_tool_name(["pnpm", "test"]) == "npm_test"
    assert normalize_tool_name(["jest"]) == "npm_test"
    # "npm run build" is not a test invocation — must not be misclassified.
    assert normalize_tool_name(["npm", "run", "build"]) == "generic"


def test_normalize_tool_name_git_subcommands():
    assert normalize_tool_name(["git", "diff"]) == "git_diff"
    assert normalize_tool_name(["git", "log", "-5"]) == "git_log"
    assert normalize_tool_name(["git", "status"]) == "git_status"
    assert normalize_tool_name(["git", "commit", "-m", "x"]) == "generic"


def test_normalize_tool_name_cargo_subcommands():
    assert normalize_tool_name(["cargo", "test"]) == "cargo_test"
    assert normalize_tool_name(["cargo", "build"]) == "cargo_build"
    assert normalize_tool_name(["cargo", "check"]) == "generic"


def test_normalize_tool_name_windows_extension_stripped():
    assert normalize_tool_name(["pytest.exe", "-k", "foo"]) == "pytest"


def test_normalize_tool_name_python_dash_m_invocation():
    # By far the most common way pytest/mypy actually get invoked — must not be
    # misclassified as "generic" just because argv[0] is the interpreter.
    assert normalize_tool_name(["python3", "-m", "pytest", "tests/", "-q"]) == "pytest"
    assert normalize_tool_name(["python3.12", "-m", "pytest"]) == "pytest"
    assert normalize_tool_name(["python", "-m", "mypy", "src/"]) == "mypy"


def test_normalize_tool_name_python_without_dash_m_is_generic():
    assert normalize_tool_name(["python3", "script.py"]) == "generic"


def test_normalize_tool_name_python_dash_m_trailing_with_no_module():
    assert normalize_tool_name(["python3", "-m"]) == "generic"


def test_normalize_tool_name_empty_argv():
    assert normalize_tool_name([]) == "generic"


def test_normalize_tool_name_unrecognized():
    assert normalize_tool_name(["make", "build"]) == "generic"


# --- _compress_generic --------------------------------------------------

def test_compress_generic_under_cap_returns_unchanged():
    text = "\n".join(f"line {i}" for i in range(10))
    assert _compress_generic(text, max_lines=200) == text


def test_compress_generic_over_cap_truncates_and_marks():
    text = "\n".join(f"line {i}" for i in range(500))
    out = _compress_generic(text, max_lines=200)
    lines = out.splitlines()
    assert len(lines) == 201  # 200 kept + 1 marker
    assert lines[0] == "line 0"
    assert "truncated" in lines[-1]
    assert "300" in lines[-1]  # 500 - 200 dropped


# --- pytest compressor ----------------------------------------------------

PYTEST_ALL_GREEN = """\
============================= test session starts ==============================
platform linux -- Python 3.11.0
collected 3 items

test_foo.py::test_a PASSED                                              [ 33%]
test_foo.py::test_b PASSED                                              [ 66%]
test_foo.py::test_c PASSED                                              [100%]

============================== 3 passed in 0.05s ==============================
"""

PYTEST_WITH_FAILURE = """\
============================= test session starts ==============================
platform linux -- Python 3.11.0
collected 2 items

test_foo.py::test_a PASSED                                              [ 50%]
test_foo.py::test_b FAILED                                              [100%]

=================================== FAILURES ===================================
_________________________________ test_b ________________________________________

    def test_b():
>       assert False
E       assert False

test_foo.py:5: AssertionError
=========================== short test summary info ============================
FAILED test_foo.py::test_b - assert False
========================= 1 failed, 1 passed in 0.12s ==========================
"""


def test_compress_pytest_all_green_collapses_to_one_line():
    out = _compress_pytest(PYTEST_ALL_GREEN, max_lines=200)
    assert out.strip() == "============================== 3 passed in 0.05s =============================="
    assert "PASSED" not in out


def test_compress_pytest_keeps_failure_detail_drops_passed_noise():
    out = _compress_pytest(PYTEST_WITH_FAILURE, max_lines=200)
    assert "PASSED" not in out
    assert "FAILURES" in out
    assert "assert False" in out
    assert "FAILED test_foo.py::test_b" in out
    assert "1 failed, 1 passed in 0.12s" in out


def test_compress_pytest_falls_back_to_generic_on_unrecognized_format():
    weird = "some pytest plugin produced\ncompletely different output\nwith no banners"
    out = _compress_pytest(weird, max_lines=200)
    assert out == weird  # under the cap, generic returns unchanged


# --- npm/jest pass-line stripping -----------------------------------------

JEST_MIXED = """\
PASS  src/foo.test.js
FAIL  src/bar.test.js
  ✓ does the happy thing (2 ms)
  ✕ does the sad thing (5 ms)

    expect(received).toBe(expected)

Tests:       1 failed, 1 passed, 2 total
Time:        1.2 s
"""


def test_compress_pass_fail_lines_drops_pass_keeps_fail():
    out = _compress_pass_fail_lines(JEST_MIXED, max_lines=200)
    assert "PASS  src/foo.test.js" not in out
    assert "does the happy thing" not in out  # ✓ line dropped
    assert "FAIL  src/bar.test.js" in out
    assert "does the sad thing" in out
    assert "Tests:       1 failed, 1 passed, 2 total" in out


# --- cargo test -------------------------------------------------------------

CARGO_TEST_OUT = """\
running 3 tests
test foo::a ... ok
test foo::b ... FAILED
test foo::c ... ok

failures:

---- foo::b stdout ----
thread 'foo::b' panicked at 'assertion failed'

test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_compress_cargo_test_drops_ok_keeps_failed():
    out = _compress_cargo_test(CARGO_TEST_OUT, max_lines=200)
    assert "test foo::a ... ok" not in out
    assert "test foo::c ... ok" not in out
    assert "test foo::b ... FAILED" in out
    assert "panicked at" in out
    assert "test result: FAILED" in out


# --- dispatch + config -----------------------------------------------------

def test_compress_dispatches_by_tool_name(tmp_path):
    out = compress("pytest", PYTEST_ALL_GREEN, tmp_path / ".memo")
    assert "PASSED" not in out


def test_compress_empty_text_returns_empty(tmp_path):
    assert compress("pytest", "", tmp_path / ".memo") == ""


def test_compress_unknown_tool_uses_generic(tmp_path):
    text = "\n".join(f"line {i}" for i in range(500))
    out = compress("some_unrecognized_tool", text, tmp_path / ".memo")
    assert "truncated" in out


def test_load_max_lines_defaults_without_config_file(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    assert _load_max_lines(root, "pytest") == 200


def test_load_max_lines_defaults_when_cache_root_missing(tmp_path):
    root = tmp_path / ".memo"  # not created
    assert _load_max_lines(root, "pytest") == 200


def test_load_max_lines_reads_toml_override(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "filters.toml").write_text("[pytest]\nmax_lines = 50\n", encoding="utf-8")
    assert _load_max_lines(root, "pytest") == 50


def test_load_max_lines_falls_back_on_malformed_toml(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "filters.toml").write_text("this is not [ valid toml", encoding="utf-8")
    assert _load_max_lines(root, "pytest") == 200


def test_load_max_lines_falls_back_on_missing_section(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "filters.toml").write_text("[eslint]\nmax_lines = 10\n", encoding="utf-8")
    assert _load_max_lines(root, "pytest") == 200
