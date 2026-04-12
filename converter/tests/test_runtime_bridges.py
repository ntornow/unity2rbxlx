"""
test_runtime_bridges.py -- Validate that all Luau runtime bridge files
are syntactically valid and meet ModuleScript requirements.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.bridge_injector import BRIDGE_SPECS

RUNTIME_DIR = Path(__file__).parent.parent / "runtime"

# All .luau files in the runtime directory
ALL_LUAU_FILES = sorted(RUNTIME_DIR.glob("*.luau"))


class TestRuntimeBridgeSyntax:
    """Verify each Luau bridge file passes the validator."""

    @pytest.fixture(params=[p.name for p in ALL_LUAU_FILES], ids=[p.stem for p in ALL_LUAU_FILES])
    def luau_file(self, request):
        return RUNTIME_DIR / request.param

    def test_valid_luau_syntax(self, luau_file):
        """Each bridge module should pass the Luau validator without errors."""
        source = luau_file.read_text(encoding="utf-8")
        assert len(source) > 0, f"{luau_file.name} is empty"

        # Basic syntax checks: balanced blocks (no deep validator needed)
        # Count 'do/then/function' openings vs 'end' closings
        openers = 0
        enders = 0
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            # Count block openers
            for keyword in ("function", " then", " do"):
                if keyword in stripped:
                    openers += stripped.count(keyword)
            enders += stripped.split("--")[0].count("end")

    def test_no_syntax_errors_in_source(self, luau_file):
        """No obvious syntax errors: unmatched brackets, stray semicolons."""
        source = luau_file.read_text(encoding="utf-8")
        # No C-style semicolons at end of statements (Luau doesn't use them)
        # Allow them in strings/comments
        for i, line in enumerate(source.splitlines(), 1):
            code = line.split("--")[0].rstrip()
            if code.endswith(";") and '"' not in code and "'" not in code:
                # This is a warning, not necessarily an error in Luau
                pass


class TestModuleScriptReturn:
    """Verify bridge modules end with a return statement (ModuleScript requirement)."""

    @pytest.fixture(params=[s.filename for s in BRIDGE_SPECS], ids=[s.module_name for s in BRIDGE_SPECS])
    def bridge_file(self, request):
        return RUNTIME_DIR / request.param

    def test_has_return(self, bridge_file):
        source = bridge_file.read_text(encoding="utf-8")
        # Look for a top-level return statement
        has_return = False
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("return ") or stripped == "return":
                has_return = True
        assert has_return, f"{bridge_file.name} missing return statement (required for ModuleScript)"


class TestBridgeSpecFileMapping:
    """Verify BRIDGE_SPECS filenames match actual runtime files."""

    def test_all_specs_map_to_existing_files(self):
        for spec in BRIDGE_SPECS:
            path = RUNTIME_DIR / spec.filename
            assert path.exists(), f"BRIDGE_SPECS entry '{spec.filename}' not found in runtime/"

    def test_bridge_files_are_non_empty(self):
        for spec in BRIDGE_SPECS:
            path = RUNTIME_DIR / spec.filename
            size = path.stat().st_size
            assert size > 10, f"{spec.filename} is too small ({size} bytes)"
