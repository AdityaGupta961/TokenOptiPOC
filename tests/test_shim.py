"""`memo.shim` — opt-in PATH shims that route a command through `memo run`.

Isolates SHIM_DIR/REGISTRY_FILE into tmp_path (mirroring test_services.py's
isolated_registry pattern) so these tests never touch the real `~/.memo/shims/`.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from memo import shim


@pytest.fixture(autouse=True)
def isolated_shim_dir(tmp_path, monkeypatch):
    shim_dir = tmp_path / "home" / ".memo" / "shims"
    monkeypatch.setattr(shim, "SHIM_DIR", shim_dir)
    monkeypatch.setattr(shim, "REGISTRY_FILE", shim_dir / "registry.json")
    yield shim_dir


@pytest.fixture
def fake_tool(tmp_path):
    """A real, executable file elsewhere on PATH, standing in for a dev tool."""
    tool_dir = tmp_path / "toolbin"
    tool_dir.mkdir()
    tool_path = tool_dir / "mytool"
    tool_path.write_text("#!/usr/bin/env sh\necho hi\n", encoding="utf-8")
    tool_path.chmod(tool_path.stat().st_mode | stat.S_IXUSR)
    return tool_dir, tool_path


def test_install_writes_shim_files_and_registry(monkeypatch, fake_tool, isolated_shim_dir):
    tool_dir, tool_path = fake_tool
    monkeypatch.setenv("PATH", str(tool_dir))

    real_target = shim.install("mytool")

    assert real_target == str(tool_path)
    assert (isolated_shim_dir / "mytool").is_file()
    assert (isolated_shim_dir / "mytool.cmd").is_file()
    assert (isolated_shim_dir / "mytool.ps1").is_file()

    body = (isolated_shim_dir / "mytool").read_text(encoding="utf-8")
    assert "memo run --" in body
    assert str(tool_path) in body

    shims = shim.list_()
    assert shims["mytool"]["realpath"] == str(tool_path)
    assert "installed_at" in shims["mytool"]


def test_install_posix_shim_is_executable(monkeypatch, fake_tool, isolated_shim_dir):
    if os.name == "nt":
        pytest.skip("POSIX executable bit is meaningless on Windows")
    tool_dir, _ = fake_tool
    monkeypatch.setenv("PATH", str(tool_dir))
    shim.install("mytool")
    mode = (isolated_shim_dir / "mytool").stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_unknown_command_raises(monkeypatch, isolated_shim_dir):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError):
        shim.install("definitely-not-a-real-command-xyz")


def test_install_refuses_to_shim_memo(isolated_shim_dir):
    with pytest.raises(ValueError, match="recurse"):
        shim.install("memo")


def test_reinstall_skips_own_shim_and_keeps_real_target(monkeypatch, fake_tool, isolated_shim_dir):
    """The chicken-and-egg case: once a shim for 'mytool' is on PATH ahead of the
    real binary, reinstalling must not record the shim's own path as its target."""
    tool_dir, tool_path = fake_tool
    monkeypatch.setenv("PATH", os.pathsep.join([str(isolated_shim_dir), str(tool_dir)]))

    first = shim.install("mytool")
    assert first == str(tool_path)

    second = shim.install("mytool")
    assert second == str(tool_path)  # not the shim's own path


def test_remove_deletes_files_and_registry_entry(monkeypatch, fake_tool, isolated_shim_dir):
    tool_dir, _ = fake_tool
    monkeypatch.setenv("PATH", str(tool_dir))
    shim.install("mytool")

    removed = shim.remove("mytool")
    assert removed is True
    assert not (isolated_shim_dir / "mytool").exists()
    assert not (isolated_shim_dir / "mytool.cmd").exists()
    assert not (isolated_shim_dir / "mytool.ps1").exists()
    assert shim.list_() == {}


def test_remove_nonexistent_shim_returns_false(isolated_shim_dir):
    assert shim.remove("never-installed") is False


def test_list_empty_when_nothing_installed(isolated_shim_dir):
    assert shim.list_() == {}


def test_is_active_true_when_shim_dir_first_on_path(monkeypatch, fake_tool, isolated_shim_dir):
    tool_dir, _ = fake_tool
    monkeypatch.setenv("PATH", str(tool_dir))
    shim.install("mytool")

    monkeypatch.setenv("PATH", os.pathsep.join([str(isolated_shim_dir), str(tool_dir)]))
    assert shim.is_active("mytool") is True


def test_is_active_false_when_shim_dir_shadowed(monkeypatch, fake_tool, isolated_shim_dir):
    tool_dir, _ = fake_tool
    monkeypatch.setenv("PATH", str(tool_dir))
    shim.install("mytool")

    # Real tool's dir comes first on PATH -> shim is installed but not in effect.
    monkeypatch.setenv("PATH", os.pathsep.join([str(tool_dir), str(isolated_shim_dir)]))
    assert shim.is_active("mytool") is False


def test_is_active_false_when_not_installed(monkeypatch, isolated_shim_dir):
    monkeypatch.setenv("PATH", "")
    assert shim.is_active("never-installed") is False
