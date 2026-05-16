"""Tests for converter/semantic_validators.py.

Each rule has a positive (should-fire) and negative (should-not-fire)
case to guard against drift in both directions. The integration test
runs the union of rules against the preserved broken Player.luau
fixture (see ``fixtures/fps_player_broken.luau``) and asserts that the
four rules the session traced (1, 2, 3, 5) all fire on it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.roblox_types import RbxScript
from converter.semantic_validators import (
    SemanticIssue,
    _strip_comments,
    run_semantic_validators,
)


def _script(name: str, source: str, *, script_type: str = "LocalScript") -> RbxScript:
    return RbxScript(name=name, source=source, script_type=script_type)


# ---------------------------------------------------------------------------
# _strip_comments
# ---------------------------------------------------------------------------


class TestStripComments:
    def test_line_comments_blanked(self) -> None:
        out = _strip_comments("local x = 1 -- a note\nfoo()")
        assert "a note" not in out
        # Offset preservation: same length, newline kept.
        assert len(out) == len("local x = 1 -- a note\nfoo()")
        assert "\nfoo()" in out

    def test_long_bracket_comments_blanked(self) -> None:
        src = "before --[[ rifle.Parent = head ]] after"
        out = _strip_comments(src)
        assert "rifle.Parent = head" not in out
        assert "before " in out and " after" in out

    def test_comments_with_anchors_dont_fire_rules(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = template:Clone()\n'
            '-- rifle.Parent = head (just an explanatory note)\n'
            'rifle.Parent = workspace\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []

    def test_double_dash_inside_quoted_string_preserved(self) -> None:
        """Codex round-8 [P3] regression: ``--`` inside a quoted
        string must NOT be treated as a comment start. The string's
        content is preserved verbatim, including the ``--``."""
        out = _strip_comments('local msg = "no -- here"; foo()')
        # The string content survives (quotes + chars).
        assert "no -- here" in out
        # The ``;`` and ``foo()`` after the string survive.
        assert "foo()" in out

    def test_double_dash_inside_long_bracket_string_preserved(self) -> None:
        """Long-bracket strings ``[[ ... ]]`` keep their content
        verbatim; an embedded ``--`` mustn't blank the rest."""
        out = _strip_comments('local m = [[note -- inside]] foo()')
        assert "note -- inside" in out
        assert "foo()" in out

    def test_real_comment_outside_string_still_blanks(self) -> None:
        """Regression: the string-tracking fix must not also disable
        legitimate comment blanking outside strings."""
        out = _strip_comments('foo() -- comment text\nbar()')
        assert "comment text" not in out
        # Subsequent line preserved.
        assert "bar()" in out


# ---------------------------------------------------------------------------
# Rule 1: weapon_clone_in_character
# ---------------------------------------------------------------------------


class TestWeaponCloneInCharacter:
    def _src(self, parent_rhs: str) -> str:
        return (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = riflePrefab:Clone()\n'
            f'rifle.Parent = {parent_rhs}\n'
        )

    def test_fires_on_head_parent(self) -> None:
        report = run_semantic_validators(
            [_script("Player", self._src("character.Head"))],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert len(report.issues) == 1
        assert report.issues[0].rule == "weapon_clone_in_character"

    def test_fires_on_weapon_slot_parent(self) -> None:
        report = run_semantic_validators(
            [_script("Player", self._src("weaponSlot.Parent"))],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert len(report.issues) == 1

    def test_no_fire_on_workspace_parent(self) -> None:
        report = run_semantic_validators(
            [_script("Player", self._src("workspace"))],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []

    def test_no_fire_without_fps_marker(self) -> None:
        src = (
            'local rifle = riflePrefab:Clone()\n'
            'rifle.Parent = character.Head\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []

    def test_fires_on_waitforchild_clone_chain(self) -> None:
        """Codex round-4 [P2] regression: the canonical prefab clone
        chain ``Templates:WaitForChild("Rifle"):Clone()`` must match."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = ReplicatedStorage:WaitForChild("Templates"):WaitForChild("Rifle"):Clone()\n'
            'rifle.Parent = character.Head\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert len(report.issues) == 1

    def test_fires_on_findfirstchild_clone_chain(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = templates:FindFirstChild("Rifle"):Clone()\n'
            'rifle.Parent = weaponSlot.Parent\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert len(report.issues) == 1

    def test_no_fire_on_non_character_primary_part(self) -> None:
        """Codex round-5 [P2] regression: ``.PrimaryPart`` alone must
        not fire — only when prefixed by ``character`` /
        ``LocalPlayer.Character``. Otherwise weapon-rig parenting
        (e.g. ``clip.Parent = weaponRig.PrimaryPart``) false-positives."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local clip = mag:Clone()\n'
            'clip.Parent = weaponRig.PrimaryPart\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []

    def test_fires_on_character_primary_part(self) -> None:
        """The qualified ``character.PrimaryPart`` still fires — that's
        a real character anchor."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = templates:FindFirstChild("Rifle"):Clone()\n'
            'rifle.Parent = character.PrimaryPart\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert len(report.issues) == 1

    def test_idempotent(self) -> None:
        scripts = [_script("Player", self._src("character.Head"))]
        a = run_semantic_validators(scripts, enabled_rules={"weapon_clone_in_character"})
        b = run_semantic_validators(scripts, enabled_rules={"weapon_clone_in_character"})
        assert len(a.issues) == len(b.issues) == 1

    def test_string_literal_fps_marker_does_not_activate_rule(self) -> None:
        """Codex round-13 [P2]: a string literal containing an FPS
        marker must not classify the script as FPS."""
        src = (
            'local hint = "Enum.MouseBehavior.LockCenter is for FPS"\n'
            'local rifle = template:Clone()\n'
            'rifle.Parent = character.Head\n'
        )
        report = run_semantic_validators(
            [_script("Helper", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []

    def test_commented_fps_marker_does_not_activate_rule(self) -> None:
        """Codex round-10 [P2] regression: an explanatory comment
        mentioning ``Enum.MouseBehavior.LockCenter`` is not enough to
        classify a script as FPS. The FPS-only rules must consult the
        comment-stripped source."""
        src = (
            '-- Note: previously we used Enum.MouseBehavior.LockCenter\n'
            'local rifle = template:Clone()\n'
            'rifle.Parent = character.Head\n'
        )
        report = run_semantic_validators(
            [_script("Helper", src)],
            enabled_rules={"weapon_clone_in_character"},
        )
        assert report.issues == []


# ---------------------------------------------------------------------------
# Rule 2: anchored_false_no_weld
# ---------------------------------------------------------------------------


class TestAnchoredFalseNoWeld:
    def test_fires_on_clone_with_no_weld(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = template:Clone()\n'
            'for _, p in rifle:GetDescendants() do\n'
            '    if p:IsA("BasePart") then p.Anchored = false end\n'
            'end\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"anchored_false_no_weld"},
        )
        assert len(report.issues) == 1

    def test_no_fire_with_weld_constraint(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = template:Clone()\n'
            'local w = Instance.new("WeldConstraint")\n'
            'p.Anchored = false\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"anchored_false_no_weld"},
        )
        assert report.issues == []

    def test_no_fire_without_clone(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'workspace.SomePart.Anchored = false\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"anchored_false_no_weld"},
        )
        assert report.issues == []

    def test_no_fire_on_string_literal_match(self) -> None:
        """Codex round-16 [P2]: a string literal containing
        ``p.Anchored = false`` must not trigger the rule."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = template:Clone()\n'
            'local msg = "use p.Anchored = false to disable physics"\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"anchored_false_no_weld"},
        )
        assert report.issues == []


# ---------------------------------------------------------------------------
# Rule 3: suspicious_meter_literal
# ---------------------------------------------------------------------------


class TestSuspiciousMeterLiteral:
    def test_fires_on_sub_meter_cframe_in_spatial_context(self) -> None:
        src = (
            'rifle:PivotTo(CFrame.new(0.4, -0.35, -0.8))\n'
            'local spawn = Vector3.new(120, 5, 47)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"suspicious_meter_literal"},
        )
        assert len(report.issues) == 1
        assert "0.4" in report.issues[0].explanation

    def test_no_fire_on_zero_vector(self) -> None:
        src = (
            'rifle:PivotTo(CFrame.new(0, 0, 0))\n'
            'local spawn = Vector3.new(120, 5, 47)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"suspicious_meter_literal"},
        )
        assert report.issues == []

    def test_no_fire_on_studs_magnitude(self) -> None:
        src = (
            'rifle:PivotTo(CFrame.new(100, 200, 300))\n'
            'local spawn = Vector3.new(120, 5, 47)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"suspicious_meter_literal"},
        )
        assert report.issues == []

    def test_no_fire_on_unit_direction(self) -> None:
        src = (
            'workspace:Raycast(origin, Vector3.new(0, 1, 0) * 5)\n'
            'local spawn = Vector3.new(120, 5, 47)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"suspicious_meter_literal"},
        )
        assert report.issues == []

    def test_no_fire_without_studs_context(self) -> None:
        src = 'rifle:PivotTo(CFrame.new(0.4, -0.35, -0.8))\n'
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"suspicious_meter_literal"},
        )
        # Lacking a studs anchor, the rule conservatively skips.
        assert report.issues == []


# ---------------------------------------------------------------------------
# Rule 4: attachment_as_render_parent
# ---------------------------------------------------------------------------


class TestAttachmentAsRenderParent:
    def test_fires_when_part_parented_to_attachment(self) -> None:
        src = (
            'local att = Instance.new("Attachment")\n'
            'local handle = Instance.new("Part")\n'
            'handle.Parent = att\n'
        )
        report = run_semantic_validators(
            [_script("Misc", src, script_type="Script")],
            enabled_rules={"attachment_as_render_parent"},
        )
        assert len(report.issues) == 1

    def test_no_fire_when_part_parented_to_workspace(self) -> None:
        src = (
            'local att = Instance.new("Attachment")\n'
            'local handle = Instance.new("Part")\n'
            'handle.Parent = workspace\n'
        )
        report = run_semantic_validators(
            [_script("Misc", src, script_type="Script")],
            enabled_rules={"attachment_as_render_parent"},
        )
        assert report.issues == []

    def test_no_fire_for_particle_emitter_under_attachment(self) -> None:
        """Codex round-3 [P3] regression: ParticleEmitter/Trail/Beam
        legitimately live under Attachments — they render via the
        attachment's WorldCFrame. The rule must only fire on BaseParts."""
        src = (
            'local att = Instance.new("Attachment")\n'
            'local emitter = Instance.new("ParticleEmitter")\n'
            'emitter.Parent = att\n'
            'local trail = Instance.new("Trail")\n'
            'trail.Parent = att\n'
            'local beam = Instance.new("Beam")\n'
            'beam.Parent = att\n'
            'local light = Instance.new("PointLight")\n'
            'light.Parent = att\n'
        )
        report = run_semantic_validators(
            [_script("FX", src, script_type="Script")],
            enabled_rules={"attachment_as_render_parent"},
        )
        assert report.issues == []

    def test_no_fire_when_lhs_class_unknown(self) -> None:
        """Conservative: if we can't trace the variable to an
        Instance.new("Part"...) call (e.g. it came from a require or a
        function argument), don't warn. Avoids false positives on
        opaque variable origins."""
        src = (
            'local att = Instance.new("Attachment")\n'
            'local mystery = getThing()\n'  # opaque source
            'mystery.Parent = att\n'
        )
        report = run_semantic_validators(
            [_script("Misc", src, script_type="Script")],
            enabled_rules={"attachment_as_render_parent"},
        )
        assert report.issues == []


# ---------------------------------------------------------------------------
# Rule 5: camera_attached_no_per_frame
# ---------------------------------------------------------------------------


class TestCameraAttachedNoPerFrame:
    def test_fires_when_pivotto_camera_with_no_heartbeat(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'rifle:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert len(report.issues) == 1

    def test_no_fire_when_heartbeat_present(self) -> None:
        # Same variable placed twice (initial + per-frame update) →
        # rule treats as legitimately driven and suppresses.
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'rifle:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
            'RunService.Heartbeat:Connect(function()\n'
            '    rifle:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
            'end)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert report.issues == []

    def test_fires_when_twice_placed_with_unrelated_heartbeat(self) -> None:
        """Codex round-15 [P2]: the variable is placed twice (equip +
        ADS toggle), AND a Heartbeat exists, but the Heartbeat doesn't
        re-place the variable. Must still fire."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'rifle:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
            'rifle:PivotTo(camera.CFrame * CFrame.new(0, 0, -4))\n'
            'RunService.Heartbeat:Connect(function(dt)\n'
            '    cooldown = cooldown + dt\n'
            'end)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert len(report.issues) == 1

    def test_fires_when_placed_twice_with_no_runservice(self) -> None:
        """Codex round-13 [P2]: two one-shot placements (e.g. on equip
        + ADS toggle) with no RunService callback still need the
        warning — the object is positioned exactly twice and never
        tracks the camera between events."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'flare:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
            'flare:PivotTo(camera.CFrame * CFrame.new(0, 0, -4))\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert len(report.issues) == 1

    def test_fires_on_multiline_camera_placement(self) -> None:
        """Codex round-12 [P3] regression: a placement split across
        lines (``rifle:PivotTo(`` on one line, ``camera.CFrame * ...``
        on the next) must still trigger the rule."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'flare:PivotTo(\n'
            '    camera.CFrame * CFrame.new(0, 0, -3)\n'
            ')\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert len(report.issues) == 1

    def test_fires_when_unrelated_heartbeat_exists(self) -> None:
        """Codex round-11 [P2] regression: an unrelated Heartbeat
        callback (e.g. bobbing/cooldowns) doesn't excuse a one-shot
        camera-attached placement of a different variable. The rule
        keys on the per-variable update count, not a global
        ``has_per_frame`` flag."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'flare:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))\n'
            'RunService.Heartbeat:Connect(function(dt)\n'
            '    cooldown = cooldown + dt\n'
            'end)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert len(report.issues) == 1

    def test_no_fire_when_camera_use_is_not_placement(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local ray = workspace:Raycast(camera.CFrame.Position, dir)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert report.issues == []

    def test_no_fire_on_camera_self_repositioning(self) -> None:
        """Codex round-4 [P2] regression: ``camera.CFrame = ...`` in
        CharacterAdded is the camera setting itself, not an object
        being attached to the camera. ``hardcoded_camera_height``
        covers the actual offset bug."""
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'localPlayer.CharacterAdded:Connect(function(char)\n'
            '    camera.CFrame = char:WaitForChild("Head").CFrame\n'
            'end)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert report.issues == []

    def test_no_fire_on_workspace_currentcamera_self_repositioning(self) -> None:
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'workspace.CurrentCamera.CFrame = CFrame.new(0, 5, 10)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        assert report.issues == []

    def test_no_fire_when_pivot_to_lives_inside_if_block_in_callback(
        self,
    ) -> None:
        """The callback-body collector used to count only ``function`` /
        ``end``, so an inner ``if ... end`` truncated the body and a
        ``rifle:PivotTo`` guarded by that ``if`` was treated as
        out-of-callback — surfacing a false ``camera_attached_no_per_frame``
        warning. Block-aware tracking must keep the PivotTo inside the
        callback body.
        """
        src = (
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
            'local rifle = riflePrefab:Clone()\n'
            'rifle:PivotTo(camera.CFrame * CFrame.new(0.5, -0.5, -3))\n'
            'RunService.RenderStepped:Connect(function(dt)\n'
            '    if rifle and rifle.Parent then\n'
            '        rifle:PivotTo(camera.CFrame * CFrame.new(0.5, -0.5, -3))\n'
            '    end\n'
            'end)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"camera_attached_no_per_frame"},
        )
        # Per-frame update via the guarded PivotTo is enough — rule
        # must NOT fire.
        assert report.issues == []


# ---------------------------------------------------------------------------
# Rule 6: hardcoded_camera_height
# ---------------------------------------------------------------------------


class TestHardcodedCameraHeight:
    def test_fires_on_sub_one_meter_offset(self) -> None:
        src = "camera.CFrame = CFrame.new(head.Position + Vector3.new(0, 0.6, 0))\n"
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"hardcoded_camera_height"},
        )
        assert len(report.issues) == 1

    def test_fires_on_workspace_currentcamera_assignment(self) -> None:
        """Codex round-14 [P2]: the transpiler can emit
        ``workspace.CurrentCamera.CFrame = ...`` in addition to the
        ``camera.CFrame = ...`` aliased form."""
        src = (
            "workspace.CurrentCamera.CFrame = "
            "CFrame.new(head.Position + Vector3.new(0, 0.6, 0))\n"
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"hardcoded_camera_height"},
        )
        assert len(report.issues) == 1

    def test_no_fire_on_studs_offset(self) -> None:
        src = "camera.CFrame = CFrame.new(head.Position + Vector3.new(0, 2.1, 0))\n"
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"hardcoded_camera_height"},
        )
        assert report.issues == []

    def test_no_cross_statement_match(self) -> None:
        """Codex round-9 [P3] regression: the DOTALL flag previously
        let an unrelated ``head.Position + Vector3.new(0, 0.6, 0)`` on
        a later line get attributed to a much earlier ``camera.CFrame =``
        assignment. Each match must stay within one statement."""
        src = (
            'camera.CFrame = CFrame.new(0, 5, 10)\n'  # studs assignment, no offset
            '-- many lines later, an unrelated offset:\n'
            'local target = head.Position + Vector3.new(0, 0.6, 0)\n'
        )
        report = run_semantic_validators(
            [_script("Player", src)],
            enabled_rules={"hardcoded_camera_height"},
        )
        # The 0.6 is in an unrelated expression — must NOT fire.
        assert report.issues == []


# ---------------------------------------------------------------------------
# Integration: broken Player fixture
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


class TestBrokenPlayerFixture:
    def test_session_rules_fire(self) -> None:
        src = _load_fixture("fps_player_broken.luau")
        report = run_semantic_validators([_script("Player", src)])
        rules = report.counts_by_rule
        # The four rules the SimpleFPS session traced.
        assert rules.get("weapon_clone_in_character", 0) >= 1, rules
        assert rules.get("anchored_false_no_weld", 0) >= 1, rules
        assert rules.get("suspicious_meter_literal", 0) >= 1, rules
        assert rules.get("camera_attached_no_per_frame", 0) >= 1, rules

    def test_to_dict_serialises(self) -> None:
        src = _load_fixture("fps_player_broken.luau")
        report = run_semantic_validators([_script("Player", src)])
        d = report.to_dict()
        assert d["total"] == len(report.issues)
        assert isinstance(d["issues"], list)
        assert all(set(i.keys()) >= {"rule", "script", "line"} for i in d["issues"])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def test_run_validators_skips_empty_scripts() -> None:
    report = run_semantic_validators([_script("Empty", "")])
    assert report.issues == []


def test_run_validators_order_deterministic() -> None:
    src = (
        'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
        'local rifle = template:Clone()\n'
        'rifle.Parent = character.Head\n'
    )
    a = run_semantic_validators([_script("Player", src)])
    b = run_semantic_validators([_script("Player", src)])
    assert [i.to_dict() for i in a.issues] == [i.to_dict() for i in b.issues]


def test_semantic_issue_roundtrips_to_dict() -> None:
    issue = SemanticIssue(
        severity="warning",
        rule="x",
        script="Y",
        line=1,
        snippet="z",
        explanation="e",
        suggested_fix="f",
    )
    d = issue.to_dict()
    assert d["rule"] == "x"
    assert d["script"] == "Y"
