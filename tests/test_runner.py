"""`memo.runner` — `memo run`'s TTY-passthrough / compression / never_worse / recall /
ledger behavior. Uses `sys.executable -c "..."` as a synthetic, cross-platform command
so these tests don't depend on pytest/git/npm actually being on the test machine's PATH
in a particular shape."""

from __future__ import annotations

import json
import sys

import pytest

from memo import runner


def _set_tty(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


def test_tty_passthrough_skips_compression_and_ledger(monkeypatch, tmp_path, capfd):
    _set_tty(monkeypatch, True)
    root = tmp_path / ".memo"
    code = runner.run_command([sys.executable, "-c", "print('hello world')"], root)
    out, _err = capfd.readouterr()
    assert code == 0
    assert "hello world" in out
    assert not (root / runner.LEDGER_FILENAME).exists()
    assert not (root / runner.RECALL_SUBDIR).exists()


def test_piped_small_output_is_never_worse_unchanged(monkeypatch, tmp_path, capfd):
    _set_tty(monkeypatch, False)
    root = tmp_path / ".memo"
    code = runner.run_command([sys.executable, "-c", "print('short output')"], root)
    out, _err = capfd.readouterr()
    assert code == 0
    assert out == "short output\n"
    assert "memo recall" not in out

    ledger_lines = (root / runner.LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    rec = json.loads(ledger_lines[0])
    assert rec["hash"] is None
    assert rec["tokens_before"] == rec["tokens_after"]
    assert rec["exit_code"] == 0


def test_piped_noisy_output_is_compressed_with_recall_pointer(monkeypatch, tmp_path, capfd):
    _set_tty(monkeypatch, False)
    root = tmp_path / ".memo"
    script = "print('\\n'.join(f'line {i}' for i in range(500)))"
    code = runner.run_command([sys.executable, "-c", script], root)
    out, _err = capfd.readouterr()
    assert code == 0
    assert "memo recall" in out
    assert out.count("\n") < 500  # meaningfully shorter than the original 500 lines

    ledger_lines = (root / runner.LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
    rec = json.loads(ledger_lines[0])
    assert rec["hash"] is not None
    assert rec["tokens_after"] < rec["tokens_before"]

    original = runner.recall(root, rec["hash"])
    assert original.count("\n") == 500  # 499 joined + print()'s own trailing newline
    assert "line 0" in original and "line 499" in original


def test_exit_code_passthrough_nonzero(monkeypatch, tmp_path, capfd):
    _set_tty(monkeypatch, False)
    root = tmp_path / ".memo"
    code = runner.run_command([sys.executable, "-c", "import sys; sys.exit(3)"], root)
    capfd.readouterr()
    assert code == 3
    ledger_lines = (root / runner.LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(ledger_lines[0])["exit_code"] == 3


def test_recall_unknown_hash_raises_keyerror(tmp_path):
    root = tmp_path / ".memo"
    with pytest.raises(KeyError):
        runner.recall(root, "a" * 64)


def test_recall_malformed_hash_raises_keyerror(tmp_path):
    root = tmp_path / ".memo"
    with pytest.raises(KeyError):
        runner.recall(root, "not-a-real-hash")


def test_resolve_executable_missing_command_raises():
    with pytest.raises(FileNotFoundError):
        runner._resolve_executable("definitely-not-a-real-command-xyz")


def test_resolve_executable_rejects_self_shim(monkeypatch, tmp_path):
    fake_memo = tmp_path / "memo"
    fake_memo.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_memo.chmod(0o755)

    def fake_which(cmd):
        return str(fake_memo) if cmd in ("mypytest", "memo") else None

    monkeypatch.setattr(runner.shutil, "which", fake_which)
    with pytest.raises(ValueError, match="recurse"):
        runner._resolve_executable("mypytest")


def test_run_command_empty_argv_raises():
    with pytest.raises(ValueError):
        runner.run_command([], None)
