"""Tests for converter.shared_state_linter -- post-transpile rewrites of
orphan ``:GetAttribute`` calls into ``require(Module).getter()`` calls."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspiledScript
from converter.shared_state_linter import lint_and_rewrite


def _ts(name: str, source: str, script_type: str = "Script") -> TranspiledScript:
    return TranspiledScript(
        source_path=f"Assets/{name}.cs",
        output_filename=f"{name}.luau",
        csharp_source="",
        luau_source=source,
        strategy="ai",
        confidence=1.0,
        script_type=script_type,
    )


class TestLintAndRewrite:
    def test_get_attribute_with_matching_set_attribute_left_alone(self) -> None:
        """When some script writes the attribute, GetAttribute is owned and stays put."""
        writer = _ts(
            "Door",
            "script:SetAttribute(\"hasKey\", true)\n",
        )
        reader = _ts(
            "Lock",
            "if character:GetAttribute(\"hasKey\") then end\n",
        )
        warnings = lint_and_rewrite([writer, reader])
        assert warnings == []
        assert "character:GetAttribute(\"hasKey\")" in reader.luau_source
        assert "require(" not in reader.luau_source

    def test_orphan_get_attribute_with_matching_getter_rewrites(self) -> None:
        """Reader uses GetAttribute, no SetAttribute anywhere, but Player exports
        a getter named hasKey -> rewrite to require(...).hasKey()."""
        player = _ts(
            "Player",
            (
                "local Player = {}\n"
                "local gotKey = false\n"
                "function Player.hasKey()\n"
                "  return gotKey\n"
                "end\n"
                "return Player\n"
            ),
            script_type="ModuleScript",
        )
        door = _ts(
            "Door",
            "if character:GetAttribute(\"hasKey\") then doorOpen = true end\n",
        )
        warnings = lint_and_rewrite([player, door])
        assert warnings == []
        assert "character:GetAttribute(\"hasKey\")" not in door.luau_source
        assert "require(script.Parent:WaitForChild(\"Player\")).hasKey()" in door.luau_source

    def test_orphan_get_attribute_with_get_prefixed_method_rewrites(self) -> None:
        """Method named getKey serves attribute "key" via the get-prefix rule."""
        player = _ts(
            "Player",
            (
                "local Player = {}\n"
                "function Player.getKey()\n  return 42\nend\n"
                "return Player\n"
            ),
            script_type="ModuleScript",
        )
        reader = _ts(
            "Lock",
            "local k = character:GetAttribute(\"key\")\n",
        )
        warnings = lint_and_rewrite([player, reader])
        assert warnings == []
        assert "require(script.Parent:WaitForChild(\"Player\")).getKey()" in reader.luau_source

    def test_orphan_with_no_writer_or_getter_emits_warning(self) -> None:
        """Pure orphan -- no rewrite possible, surfaces in UNCONVERTED.md."""
        reader = _ts(
            "Hud",
            "local s = char:GetAttribute(\"score\")\n",
        )
        warnings = lint_and_rewrite([reader])
        assert len(warnings) == 1
        assert warnings[0]["category"] == "shared_state"
        assert "Hud:GetAttribute(\"score\")" in warnings[0]["item"]
        # Source untouched -- we don't have a fix to apply.
        assert "char:GetAttribute(\"score\")" in reader.luau_source

    def test_unrelated_code_unchanged(self) -> None:
        """Scripts without GetAttribute calls should not be touched."""
        plain = _ts(
            "Util",
            "local x = 1\nprint('hi')\n",
        )
        before = plain.luau_source
        warnings = lint_and_rewrite([plain])
        assert warnings == []
        assert plain.luau_source == before

    def test_self_reference_not_rewritten(self) -> None:
        """A module that defines its own getter should not require() itself."""
        player = _ts(
            "Player",
            (
                "local Player = {}\n"
                "function Player.hasKey()\n  return script:GetAttribute(\"hasKey\")\nend\n"
                "return Player\n"
            ),
            script_type="ModuleScript",
        )
        warnings = lint_and_rewrite([player])
        # No matching SetAttribute anywhere, so the attribute is orphan,
        # but rewriting Player.hasKey to require(...Player).hasKey() would
        # be a self-cycle -- skip the rewrite. We also flag the orphan once.
        assert "require(script.Parent:WaitForChild(\"Player\")).hasKey()" not in player.luau_source
        assert any(w["item"].startswith("Player:GetAttribute") for w in warnings)

    def test_duplicate_orphan_call_sites_dedup_in_warnings(self) -> None:
        """Multiple GetAttribute calls for the same attr from one script
        produce a single UNCONVERTED entry, not one per call site."""
        reader = _ts(
            "Hud",
            (
                "local a = char:GetAttribute(\"score\")\n"
                "local b = char:GetAttribute(\"score\")\n"
                "if other:GetAttribute(\"score\") then end\n"
            ),
        )
        warnings = lint_and_rewrite([reader])
        assert len(warnings) == 1
        assert warnings[0]["item"] == "Hud:GetAttribute(\"score\")"

    def test_module_without_return_skipped(self) -> None:
        """A ModuleScript without a `return Tbl` line is not treated as exporting."""
        player = _ts(
            "Player",
            (
                "local Player = {}\n"
                "function Player.hasKey() return true end\n"
                # no return Player at end
            ),
            script_type="ModuleScript",
        )
        reader = _ts("Door", "local k = c:GetAttribute(\"hasKey\")\n")
        warnings = lint_and_rewrite([player, reader])
        # No exporter recognized -> orphan.
        assert len(warnings) == 1
        assert "require(" not in reader.luau_source
