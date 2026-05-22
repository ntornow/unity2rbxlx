"""
test_unbound_script_guard.py -- Policy tests for pipeline._disable_unbound_scripts.

The BasePart guard at pipeline.py:3770-3810 wraps unbound scripts with
``if not script.Parent:IsA("BasePart") then return end`` so a Server
script routed to ServerScriptService doesn't crash when it dereferences
``script.Parent.Position``. The guard is correct for that case and wrong
for several others:

  * LocalScripts routed to StarterPlayerScripts / StarterCharacterScripts
    where ``script.Parent`` is the PlayerScripts/Character container —
    not a BasePart, but the script doesn't *crash*, it just silently
    no-ops everything when guarded.
  * Self-guarded scripts that already check ``script.Parent:IsA("Model")``
    or ``script.Parent:IsA("BasePart")`` themselves.

The fix (Option D, 2026-05-21) gates the guard on
:attr:`RbxScript.requires_part_parent` — computed once in coherence by
``_detect_part_parent_requirement`` — plus a parent_path allowlist of
non-Part client/character containers. These tests pin that policy.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxPart, RbxPlace, RbxScript  # noqa: E402
from converter.pipeline import Pipeline  # noqa: E402


def _make_pipeline(tmp_path: Path) -> Pipeline:
    """Build a Pipeline against a minimal Unity project layout with an
    empty RbxPlace allocated up front (write_output normally creates
    this; tests skip straight to the subphase under test)."""
    (tmp_path / "Assets").mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(unity_project_path=tmp_path, output_dir=tmp_path / "out")
    pipeline.state.rbx_place = RbxPlace()
    return pipeline


class TestUnboundScriptGuardPolicy:
    """The four cases that define the guard's contract."""

    def test_self_guarded_script_skips_baseparts_guard(self, tmp_path: Path) -> None:
        """A smart-binding script that already self-guards via
        ``script.Parent:IsA("Model")`` must not get the unconditional
        BasePart guard prepended — that would short-circuit the
        script's own conditional before it runs, breaking both the
        flat-list and template-attached copies."""
        pipeline = _make_pipeline(tmp_path)
        smart_source = (
            "if script.Parent and (script.Parent:IsA('Model') "
            "or script.Parent:IsA('BasePart')) then\n"
            "  local target = script.Parent:FindFirstChild('Vehicle', true)\n"
            "else\n"
            "  local target = workspace:FindFirstChild('Vehicle', true)\n"
            "end\n"
        )
        anim_script = RbxScript(
            name="Anim_Vehicle_Wheel_Spin",
            source=smart_source,
            script_type="Script",
            parent_path="ServerScriptService",
        )
        pipeline.state.rbx_place.scripts.append(anim_script)
        pipeline.state.rbx_place.workspace_parts.append(
            RbxPart(name="Anchor", class_name="Part"),
        )

        pipeline._bind_scripts_to_parts()

        assert 'if not script.Parent:IsA("BasePart") then return end' not in anim_script.source, (
            "self-guarded script must not receive the BasePart-only guard; "
            "full source:\n" + anim_script.source
        )

    def test_localscript_routed_to_starter_player_scripts_is_not_guarded(self, tmp_path: Path) -> None:
        """Regression for the BasePart-guard interaction bug discovered
        2026-05-21: a LocalScript routed to StarterPlayerScripts whose
        source contains ``script.Parent:FindFirstChild(...)`` (a
        defensive sound lookup) MUST NOT receive the BasePart guard —
        ``script.Parent`` is ``PlayerScripts`` at runtime, never a
        BasePart, so the guard would silently no-op the entire client
        Player controller. The old regex-based guard caught this case
        and broke all gameplay non-deterministically based on whether
        the AI happened to emit the pattern in Player.luau.

        Phase 1 (the analyzer) sets ``requires_part_parent=False`` for
        this script because ``:FindFirstChild`` works on any Instance.
        Phase 2 (this guard) reads that flag and skips the wrap.
        """
        pipeline = _make_pipeline(tmp_path)
        player_lua = RbxScript(
            name="Player",
            source=(
                "local UserInputService = game:GetService('UserInputService')\n"
                "local function setupSounds()\n"
                "    local s = script.Parent:FindFirstChild('ShootSound')\n"
                "    if s then s:Play() end\n"
                "end\n"
                "UserInputService.InputBegan:Connect(function() print('moved') end)\n"
            ),
            script_type="LocalScript",
            parent_path="StarterPlayer.StarterPlayerScripts",
            requires_part_parent=False,  # analyzer would set this
        )
        pipeline.state.rbx_place.scripts.append(player_lua)

        pipeline._bind_scripts_to_parts()

        assert 'if not script.Parent:IsA("BasePart") then return end' not in player_lua.source, (
            "LocalScript routed to PlayerScripts must not be guard-wrapped; "
            "the guard would silently disable the entire client. Source:\n"
            + player_lua.source
        )

    def test_localscript_with_requires_part_parent_in_starter_player_warns(self, tmp_path: Path) -> None:
        """When a script is flagged ``requires_part_parent=True`` but
        ends up routed to a client/character container (a converter
        misconfiguration), the guard should NOT silently disable it.
        Instead, surface a build-time warning so the misroute is
        visible in the conversion report — silent failures are the
        bug class this whole gate change was designed to eliminate."""
        pipeline = _make_pipeline(tmp_path)
        misrouted = RbxScript(
            name="MisroutedPartScript",
            source="print(script.Parent.Position)\n",
            script_type="LocalScript",
            parent_path="StarterPlayer.StarterPlayerScripts",
            requires_part_parent=True,
        )
        pipeline.state.rbx_place.scripts.append(misrouted)

        pipeline._bind_scripts_to_parts()

        # Guard must NOT silently disable — let it crash visibly so we know.
        assert 'if not script.Parent:IsA("BasePart") then return end' not in misrouted.source
        # And a warning explaining the misroute should be in ctx.
        assert any("MisroutedPartScript" in w and "requires_part_parent" in w
                   for w in pipeline.ctx.warnings), (
            f"expected a misroute warning for MisroutedPartScript, got: "
            f"{pipeline.ctx.warnings}"
        )

    def test_server_script_requiring_part_routed_to_sss_still_guarded(self, tmp_path: Path) -> None:
        """The case that originally motivated the guard: a Server
        Script that genuinely accesses ``script.Parent.Position`` and
        lands in ServerScriptService (where ``script.Parent`` is the
        service). Without the guard, that script would crash on every
        Heartbeat. The gate change preserves this behavior — gated on
        requires_part_parent=True so only scripts that ACTUALLY need a
        Part parent receive the wrap."""
        pipeline = _make_pipeline(tmp_path)
        orphan = RbxScript(
            name="OrphanPhysicsForce",
            source="while true do print(script.Parent.Position) task.wait() end\n",
            script_type="Script",
            parent_path="ServerScriptService",
            requires_part_parent=True,
        )
        pipeline.state.rbx_place.scripts.append(orphan)

        pipeline._bind_scripts_to_parts()

        assert orphan.source.startswith(
            "-- Guard: this script expects script.Parent to be a BasePart\n"
            'if not script.Parent:IsA("BasePart") then return end\n'
        ), (
            "server script needing a Part parent + landing in SSS must "
            "still receive the guard; source:\n" + orphan.source
        )
