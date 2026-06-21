#!/usr/bin/env python3
"""Fail-closed gate for the cold-e2e nightly: assert the AI transpile ran.

The cold-e2e job converts SimpleFPS with AI ON and no ANTHROPIC_API_KEY, so the
`claude` CLI is the only AI backend. If it is off the runner PATH the convert
silently produces an all-stub conversion (`u2r.py convert` still exits 0): it
boots on Roblox's default Humanoid controls but the paradigm-C player/camera
controller never binds, surfacing misleadingly as `mouse_moves_view=false` at the
bind step.

This gate reads `conversion_report.json` and asserts the POSITIVE signal — the
converter's own `scripts.ai_transpiled` count must be > 0 — plus a secondary
check that no component module fell through to the stub strategy. It is
LOAD-BEARING and **fails closed**: any anomaly (missing/unreadable/malformed
report, a non-int count, a count <= 0, or a stub fallthrough) exits non-zero
with a GitHub `::error::` annotation. Keying on the positive count (not mere
absence of a stub_strategy error) is robust because visual-only / inert stubs
are error-exempt.

Usage:  assert_ai_transpiled.py <path/to/conversion_report.json>
Exit:   0 = a real AI transpile ran; 1 = AI unavailable / report anomaly.
"""
from __future__ import annotations

import json
import sys


def _err(msg: str) -> int:
    print(f"::error::cold-AI gate: {msg}")
    return 1


def check(report_path: str) -> int:
    try:
        with open(report_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _err(
            f"conversion_report.json missing at {report_path} — the convert did "
            f"not complete."
        )
    except (OSError, ValueError) as exc:
        return _err(
            f"conversion_report.json at {report_path} is unreadable/malformed "
            f"({exc}) — failing closed."
        )

    if not isinstance(data, dict):
        return _err("conversion_report.json is not a JSON object — failing closed.")

    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return _err("report.scripts is missing or not an object — failing closed.")

    ai = scripts.get("ai_transpiled")
    # bool is an int subclass — reject it explicitly so True/False can't pass.
    if not isinstance(ai, int) or isinstance(ai, bool):
        return _err(
            f"scripts.ai_transpiled is not an integer ({ai!r}) — failing closed."
        )

    errors = data.get("errors")
    if errors is None:
        errors = []
    if not isinstance(errors, list):
        return _err("report.errors is not a list — failing closed.")
    stub_errors = [
        e for e in errors if "stub_strategy" in str(e) or "AI unavailable" in str(e)
    ]

    print(
        f"scripts.ai_transpiled={ai}; "
        f"stub_strategy/AI-unavailable errors={len(stub_errors)}"
    )

    if ai <= 0:
        return _err(
            "0 AI transpiles — the AI backend was unavailable and the conversion "
            "is all-stub. This is the real failure (mouse_moves_view=false "
            "downstream is only its symptom). Check that the 'claude' CLI is on "
            "the runner PATH."
        )

    if stub_errors:
        return _err(
            f"{len(stub_errors)} component module(s) fell through to the stub "
            f"strategy (AI unavailable) — see conversion_report.json errors[]."
        )

    print(f"AI transpile confirmed (ai_transpiled={ai}, no stub fallthrough).")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_ai_transpiled.py <conversion_report.json>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
