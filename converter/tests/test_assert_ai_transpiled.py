"""Unit tests for tools/assert_ai_transpiled.py — the cold-e2e fail-closed gate.

Proves the gate FAILS CLOSED (exit 1) on every anomaly and only passes (exit 0)
on a real AI transpile, so a green check genuinely means AI ran.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.assert_ai_transpiled import check


def _write(tmp_path: Path, payload) -> str:
    p = tmp_path / "conversion_report.json"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_pass_on_real_ai_transpile(tmp_path: Path) -> None:
    report = _write(tmp_path, {"scripts": {"ai_transpiled": 12}, "errors": []})
    assert check(report) == 0


def test_pass_when_errors_absent(tmp_path: Path) -> None:
    # errors key omitted entirely -> treated as empty, still passes on ai>0.
    report = _write(tmp_path, {"scripts": {"ai_transpiled": 1}})
    assert check(report) == 0


def test_fail_on_zero_ai_transpiled(tmp_path: Path) -> None:
    # The exact 2026-06-20 failure shape.
    report = _write(
        tmp_path,
        {
            "scripts": {"ai_transpiled": 0},
            "errors": ["… fell through to 'stub' strategy (AI unavailable). …"],
        },
    )
    assert check(report) == 1


def test_fail_on_stub_strategy_error_even_if_ai_positive(tmp_path: Path) -> None:
    report = _write(
        tmp_path,
        {
            "scripts": {"ai_transpiled": 5},
            "errors": [
                "scene-runtime contract failed closed (stub_strategy): Player.cs: …"
            ],
        },
    )
    assert check(report) == 1


def test_fail_closed_on_missing_file(tmp_path: Path) -> None:
    assert check(str(tmp_path / "does_not_exist.json")) == 1


def test_fail_closed_on_malformed_json(tmp_path: Path) -> None:
    report = _write(tmp_path, "{not valid json")
    assert check(report) == 1


@pytest.mark.parametrize("bad", ["12", None, True, 1.5, [], {}])
def test_fail_closed_on_non_int_ai_transpiled(tmp_path: Path, bad) -> None:
    # A non-int count (incl. bool and the string "12") must fail closed, not be
    # coerced — this is the bypass the shell `[ -le ]` integer test allowed.
    report = _write(tmp_path, {"scripts": {"ai_transpiled": bad}, "errors": []})
    assert check(report) == 1


def test_fail_closed_on_missing_scripts(tmp_path: Path) -> None:
    report = _write(tmp_path, {"errors": []})
    assert check(report) == 1


def test_fail_closed_on_non_object_report(tmp_path: Path) -> None:
    report = _write(tmp_path, [1, 2, 3])
    assert check(report) == 1


def test_fail_closed_on_non_list_errors(tmp_path: Path) -> None:
    report = _write(tmp_path, {"scripts": {"ai_transpiled": 3}, "errors": "oops"})
    assert check(report) == 1
