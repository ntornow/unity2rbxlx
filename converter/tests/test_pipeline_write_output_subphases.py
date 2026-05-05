"""Tests for the write_output() subphase split.

Catches three regressions:
  1. ``Pipeline.SUBPHASE_ORDER`` and the actual call sequence in
     ``write_output()`` drifting apart. The order is load-bearing —
     classify_storage must run after cohere_scripts, autogen scripts
     must run after FPS detection, etc.
  2. A subphase method getting renamed without updating SUBPHASE_ORDER
     (would silently break the orchestrator).
  3. Each subphase being callable as a method (not accidentally
     replaced by a free function or removed).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from converter.pipeline import Pipeline


PIPELINE_PATH = Path(__file__).parent.parent / "converter" / "pipeline.py"


def _parse_write_output_call_sequence() -> list[str]:
    """Extract the ordered list of self.<method>() calls from write_output's
    AST. Skips logging/control-flow calls; only counts subphase invocations
    that match either the _subphase_* prefix or one of the existing
    helper-method names that participate in the orchestration."""
    tree = ast.parse(PIPELINE_PATH.read_text())
    ORCHESTRATION_PATTERNS = {
        "_classify_storage",
        "_bind_scripts_to_parts",
        "_inject_runtime_modules",
        "_generate_prefab_packages",
    }
    calls: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "write_output"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "self":
                continue
            name = func.attr
            if name.startswith("_subphase_") or name in ORCHESTRATION_PATTERNS:
                calls.append(name)
        break  # only inspect the first write_output found
    return calls


class TestSubphaseOrderInvariant:
    """Pipeline.SUBPHASE_ORDER documents the canonical sequence. The actual
    write_output() body must call methods in exactly that order — drift
    breaks ordering-sensitive behaviors silently."""

    def test_subphase_order_constant_is_defined(self) -> None:
        assert hasattr(Pipeline, "SUBPHASE_ORDER")
        assert isinstance(Pipeline.SUBPHASE_ORDER, tuple)
        assert len(Pipeline.SUBPHASE_ORDER) >= 7  # don't care about exact count, but ≥7

    def test_actual_call_order_matches_constant(self) -> None:
        actual = _parse_write_output_call_sequence()
        expected = list(Pipeline.SUBPHASE_ORDER)
        assert actual == expected, (
            "write_output() call sequence drifted from SUBPHASE_ORDER.\n"
            f"  declared: {expected}\n"
            f"  actual:   {actual}"
        )

    def test_every_listed_subphase_is_a_method(self) -> None:
        """SUBPHASE_ORDER references methods that must exist on Pipeline.
        Catches a regression where a phase is removed but not unlisted."""
        for name in Pipeline.SUBPHASE_ORDER:
            method = getattr(Pipeline, name, None)
            assert method is not None, (
                f"SUBPHASE_ORDER lists {name!r} but Pipeline has no such method"
            )
            assert callable(method)
            # Must be an instance method (takes `self`)
            sig = inspect.signature(method)
            assert "self" in sig.parameters, (
                f"{name} should be an instance method"
            )


class TestSubphaseDocstrings:
    """Each extracted _subphase_* method must explain what it does so a
    future contributor can identify which subphase to touch when adding
    a new feature."""

    def test_extracted_subphases_have_docstrings(self) -> None:
        extracted = [n for n in Pipeline.SUBPHASE_ORDER if n.startswith("_subphase_")]
        assert extracted, "no _subphase_ entries in SUBPHASE_ORDER — extraction undone?"
        for name in extracted:
            method = getattr(Pipeline, name)
            assert method.__doc__ is not None and method.__doc__.strip(), (
                f"{name} is missing a docstring"
            )


class TestWriteOutputShape:
    """write_output() should be a thin orchestrator after the refactor.
    A regression that re-inlines a subphase would balloon this back to
    hundreds of lines."""

    def test_write_output_is_short(self) -> None:
        """write_output should fit in ~150 lines (was 683 pre-refactor).
        Allow generous headroom — the goal is to catch a wholesale
        re-inlining, not nitpick line counts."""
        src = inspect.getsource(Pipeline.write_output)
        line_count = src.count("\n")
        assert line_count < 250, (
            f"write_output is {line_count} lines — likely re-inlined a subphase. "
            f"Threshold: 250."
        )
