"""`memo.config.load_billing_mode` — gates Layer 1's compression aggressiveness."""

from __future__ import annotations

import json

from memo.config import DEFAULT_BILLING_MODE, load_billing_mode


def test_billing_mode_defaults_when_no_config(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    assert load_billing_mode(root) == DEFAULT_BILLING_MODE == "subscription"


def test_billing_mode_defaults_when_no_cache_root(tmp_path):
    # cache_root itself need not exist yet — must not raise.
    root = tmp_path / ".memo"
    assert load_billing_mode(root) == "subscription"


def test_billing_mode_reads_valid_override(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"billing_mode": "payg"}), encoding="utf-8")
    assert load_billing_mode(root) == "payg"


def test_billing_mode_is_case_insensitive(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"billing_mode": "PAYG"}), encoding="utf-8")
    assert load_billing_mode(root) == "payg"


def test_billing_mode_falls_back_on_unknown_value(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"billing_mode": "enterprise"}), encoding="utf-8")
    assert load_billing_mode(root) == "subscription"


def test_billing_mode_falls_back_on_malformed_json(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "config.json").write_text("{not valid json", encoding="utf-8")
    assert load_billing_mode(root) == "subscription"


def test_billing_mode_ignores_unrelated_config_keys(tmp_path):
    root = tmp_path / ".memo"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"extensions": [".py"]}), encoding="utf-8")
    assert load_billing_mode(root) == "subscription"
