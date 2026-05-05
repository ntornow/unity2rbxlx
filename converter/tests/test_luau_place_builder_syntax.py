"""Tests for luau_place_builder.generate_place_luau output syntax.

The headless Luau Execution API consumes a single Luau script that
recreates the entire place. A regression that produces invalid Luau
syntax breaks the upload path silently — the script is uploaded, fails
during execution, and the reconstructed place is empty or partial.

Tests run the generated script through `luau-analyze` (the same tool
the transpiler uses for syntax validation). Marked @slow because
luau-analyze startup overhead is ~0.5 s per invocation.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from core.roblox_types import (
    RbxCFrame,
    RbxLightingConfig,
    RbxPart,
    RbxPlace,
    RbxScript,
)
from roblox.luau_place_builder import generate_place_luau


pytestmark = pytest.mark.slow

requires_luau_analyze = pytest.mark.skipif(
    shutil.which("luau-analyze") is None,
    reason="luau-analyze not in PATH",
)


def _check_luau_syntax(source: str) -> tuple[int, str]:
    """Run luau-analyze on a script and return (returncode, stderr)."""
    proc = subprocess.run(
        ["luau-analyze", "-"],
        input=source,
        text=True,
        capture_output=True,
        timeout=60,
    )
    # luau-analyze prints diagnostics to stdout, not stderr
    return proc.returncode, proc.stdout + proc.stderr


def _minimal_place() -> RbxPlace:
    """Smallest place with a couple of parts and a script."""
    return RbxPlace(
        workspace_parts=[
            RbxPart(
                name="Ground",
                class_name="Part",
                cframe=RbxCFrame(),
                size=(100.0, 1.0, 100.0),
                color=(0.5, 0.5, 0.5),
                anchored=True,
            ),
            RbxPart(
                name="Spawn",
                class_name="Part",
                cframe=RbxCFrame(x=0.0, y=5.0, z=0.0),
                size=(4.0, 1.0, 4.0),
                anchored=True,
            ),
        ],
        scripts=[
            RbxScript(
                name="HelloServer",
                source='print("hello from server")\n',
                script_type="Script",
            ),
        ],
        lighting=RbxLightingConfig(),
    )


@requires_luau_analyze
class TestGenerateValidLuau:
    def test_minimal_place_parses(self) -> None:
        place = _minimal_place()
        source = generate_place_luau(place)
        rc, output = _check_luau_syntax(source)
        assert rc == 0, (
            f"luau-analyze rejected the generated script (exit {rc}):\n"
            f"---\n{output}\n---\nSCRIPT (first 800 chars):\n{source[:800]}"
        )

    def test_empty_place_parses(self) -> None:
        """Edge case: place with no parts, no scripts. The header + footer
        Luau must still parse cleanly."""
        place = RbxPlace()
        source = generate_place_luau(place)
        rc, output = _check_luau_syntax(source)
        assert rc == 0, f"luau-analyze rejected empty-place script:\n{output}"

    def test_place_with_special_chars_in_names_parses(self) -> None:
        """Part names containing characters that need escaping in Luau
        strings (quotes, backslashes, newlines) must be properly emitted —
        a regression that fails to escape produces unparseable Luau."""
        place = RbxPlace(
            workspace_parts=[
                RbxPart(
                    name='Part "with" quotes',
                    class_name="Part",
                    cframe=RbxCFrame(),
                    size=(1.0, 1.0, 1.0),
                    anchored=True,
                ),
                RbxPart(
                    name="Part\\with\\backslashes",
                    class_name="Part",
                    cframe=RbxCFrame(),
                    size=(1.0, 1.0, 1.0),
                    anchored=True,
                ),
            ],
        )
        source = generate_place_luau(place)
        rc, output = _check_luau_syntax(source)
        assert rc == 0, f"escaping bug in name emission:\n{output}"

    def test_scripts_with_internal_quotes_parse(self) -> None:
        """Script source containing string-delimiter chars must round-trip
        through whatever quoting/[[long-string]] strategy the builder uses."""
        place = RbxPlace(
            scripts=[
                RbxScript(
                    name="Tricky",
                    source='print("hello"); print([[long string]])\n',
                    script_type="Script",
                ),
            ],
        )
        source = generate_place_luau(place)
        rc, output = _check_luau_syntax(source)
        assert rc == 0, f"script quoting regression:\n{output}"


class TestGenerateStructure:
    """Lightweight structural checks that don't require luau-analyze."""

    def test_returns_non_empty_string(self) -> None:
        source = generate_place_luau(_minimal_place())
        assert isinstance(source, str)
        assert source

    def test_includes_required_services(self) -> None:
        """The header must request the services the rest of the script
        references — without them, the script parses but errors at
        runtime when accessing AssetService etc."""
        source = generate_place_luau(_minimal_place())
        for service in (
            "AssetService",
            "Lighting",
            "ServerScriptService",
            "ServerStorage",
            "ReplicatedStorage",
            "StarterPlayer",
            "StarterGui",
        ):
            assert f'GetService(\'{service}\')' in source or \
                   f'GetService("{service}")' in source, \
                f"required service {service} missing from generated script"

    def test_part_names_appear_in_output(self) -> None:
        place = _minimal_place()
        source = generate_place_luau(place)
        for part in place.workspace_parts:
            assert part.name in source, f"part name {part.name!r} missing"
