"""Tests for the movement-facet lowering pass (generic allowlist).

Player IDENTITY comes from the deterministic UPSTREAM Unity signal -- the
planner's per-module ``has_character_controller`` flag (a script co-located
with a Unity ``CharacterController``), NOT a fingerprint of the transpiled
output. Once identified, the WASD move method is LOCATED within that one script
(>=3 distinct ``Enum.KeyCode.WASD`` refs -- inline OR helper-wrapped -- plus a
locomotion side-effect) and whole-body-replaced onto the character's
``Humanoid:Move``; the camera pass emits ``followCharacter = true`` for it.

Fixtures cover BOTH the strict daa09e shape AND the real dde248 shape that
defeated the retired fingerprint (helper-wrapped ``_axis(Enum.KeyCode.D, ...)``
WASD + an extra yaw term in the camera CFrame), exercised through the REAL
find_player_controllers -> lower_camera_facet -> lower_movement_facet ordering.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.camera_facet_lowering import lower_camera_facet  # noqa: E402
from converter.code_transpiler import (  # noqa: E402
    TranspilationResult,
    TranspiledScript,
)
from converter.movement_facet_lowering import (  # noqa: E402
    find_player_controllers,
    lower_movement_facet,
)


class _S:
    """Minimal TranspiledScript stand-in. ``source_path`` is what joins the
    script to its planner module (by file stem); ``name`` is present to PROVE
    identification never reads it (identity is the upstream module flag)."""

    def __init__(self, src: str, name: str = "Player") -> None:
        self.luau_source = src
        self.name = name
        self.source_path = f"Assets/{name}.cs"
        self.output_filename = f"{name}.luau"


def _row(stem: str, has_cc: bool) -> dict:
    return {
        "stem": stem,
        "class_name": stem,
        "runtime_bearing": True,
        "is_component_class": True,
        "character_attached": False,
        "is_loader": False,
        "has_character_controller": has_cc,
    }


def _modules(cc: tuple[str, ...] = (), plain: tuple[str, ...] = ()) -> dict:
    """Build a ``scene_runtime.modules`` dict: ``cc`` stems carry the upstream
    CharacterController flag (player candidates); ``plain`` stems do not."""
    mods: dict[str, dict] = {}
    for stem in cc:
        mods[f"guid-{stem}"] = _row(stem, has_cc=True)
    for stem in plain:
        mods[f"guid-{stem}"] = _row(stem, has_cc=False)
    return mods


# --- Verbatim daa09e shapes (strict camera shape) --------------------------

_AWAKE = textwrap.dedent("""\
    function Player:Awake()
        Player.instance = self.gameObject
        self.source = self:GetComponent("AudioSource")
        self.control = self:GetComponent("CharacterController")
        self.cam = workspace.CurrentCamera
        self.weaponSlot = self.cam and self.cam:GetChildren()[1]
    end
""")

# Rotate -- strict flattened FPS shape (yaw-only body turn + pitch-only cam).
_ROTATE = textwrap.dedent("""\
    function Player:Rotate(dt)
        local UIS = game:GetService("UserInputService")
        local delta = UIS:GetMouseDelta()
        local yaw = self.sensitivity * dt * delta.X
        self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))
        self.camRotationX = self.camRotationX - delta.Y * self.sensitivity * dt
        self.camRotationX = math.clamp(self.camRotationX, self.minAngle, self.maxAngle)
        if self.cam then
            local pos = self.cam.CFrame.Position
            self.cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), 0, 0)
        end
    end
""")

# Move -- WASD inline, ``self.gameObject:PivotTo`` locomotion.
_MOVE = textwrap.dedent("""\
    function Player:Move(dt)
        local UIS = game:GetService("UserInputService")

        local grounded = true
        if self.control and self.control:IsA("Humanoid") then
            grounded = self.control.FloorMaterial ~= Enum.Material.Air
        end

        if grounded then
            local h = 0
            if UIS:IsKeyDown(Enum.KeyCode.D) then h = h + 1 end
            if UIS:IsKeyDown(Enum.KeyCode.A) then h = h - 1 end
            local v = 0
            if UIS:IsKeyDown(Enum.KeyCode.W) then v = v + 1 end
            if UIS:IsKeyDown(Enum.KeyCode.S) then v = v - 1 end

            local md = self.gameObject:GetPivot():VectorToWorldSpace(Vector3.new(h, 0, v))
            self.moveDir = md

            if UIS:IsKeyDown(Enum.KeyCode.Space) then
                self.moveDir = Vector3.new(self.moveDir.X, self.jumpSpeed, self.moveDir.Z)
            end
        end

        self.moveDir = Vector3.new(self.moveDir.X, self.moveDir.Y - self.gravity * dt, self.moveDir.Z)
        local disp = self.moveDir * self.speed * dt * STUDS_PER_METER
        self.gameObject:PivotTo(self.gameObject:GetPivot() + disp)
    end
""")


def _player_src() -> str:
    return (
        "local Player = {}\nPlayer.__index = Player\n\n"
        + _AWAKE + "\n" + _ROTATE + "\n" + _MOVE + "\nreturn Player\n"
    )


# --- The REAL dde248 shape that defeated the retired fingerprint -----------

# Rotate -- the AI emitted an extra ``(basePivot - basePivot.Position)`` yaw
# term between CFrame.new and CFrame.Angles, defeating the strict _CAM_PITCH_RE.
# The broadened locator keys on GetMouseDelta() + camera ownership instead.
_REAL_ROTATE = textwrap.dedent("""\
    function Player:Rotate(dt)
        local mouseDelta = self.uis:GetMouseDelta()
        local yaw = self.sensitivity * dt * mouseDelta.X
        self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))
        local x = self.camRotation.X - mouseDelta.Y * self.sensitivity * dt
        x = math.clamp(x, self.minAngle, self.maxAngle)
        self.camRotation = Vector3.new(x, 0, 0)
        local cam = workspace.CurrentCamera
        if cam then
            cam.CameraType = Enum.CameraType.Scriptable
            local basePivot = self.gameObject:GetPivot()
            cam.CFrame = CFrame.new(basePivot.Position) * (basePivot - basePivot.Position) * CFrame.Angles(-math.rad(x), 0, 0)
        end
    end
""")

# Move -- WASD factored through ``self:_axis(Enum.KeyCode.D, Enum.KeyCode.A)``
# helpers (zero inline IsKeyDown literals), drives the Humanoid directly.
_REAL_MOVE = textwrap.dedent("""\
    function Player:Move(dt)
        local humanoid = self.control
        if not humanoid then
            return
        end
        local grounded = humanoid.FloorMaterial ~= Enum.Material.Air
        if grounded then
            local h = self:_axis(Enum.KeyCode.D, Enum.KeyCode.A)
            local v = self:_axis(Enum.KeyCode.W, Enum.KeyCode.S)
            local localDir = Vector3.new(h, 0, -v)
            self.moveDirection = self.gameObject:GetPivot():VectorToWorldSpace(localDir)
            if self:_keyDown(Enum.KeyCode.Space) then
                humanoid.Jump = true
            end
        end
        if self.moveDirection.Magnitude > 0 then
            humanoid:Move(self.moveDirection.Unit, false)
        else
            humanoid:Move(Vector3.zero, false)
        end
    end
""")

# The _axis / _keyDown helpers read keys through a VARIABLE -- they must NOT be
# mistaken for the move method (no literal KeyCode refs, no locomotion).
_HELPERS = textwrap.dedent("""\
    function Player:_axis(posCode, negCode)
        local a = 0
        if self.uis:IsKeyDown(posCode) then a = a + 1 end
        if self.uis:IsKeyDown(negCode) then a = a - 1 end
        return a
    end

    function Player:_keyDown(code)
        return self.uis:IsKeyDown(code)
    end
""")


def _real_player_src() -> str:
    return (
        "local Player = {}\nPlayer.__index = Player\n\n"
        + _AWAKE + "\n" + _REAL_ROTATE + "\n" + _REAL_MOVE + "\n" + _HELPERS
        + "\nreturn Player\n"
    )


class TestPositive:
    def test_find_camera_movement_ordering(self) -> None:
        """Real identify -> camera -> movement ordering on the daa09e shape."""
        s = _S(_player_src())
        scripts = [s]
        modules = _modules(cc=("Player",))

        players = find_player_controllers(scripts, modules)
        assert players == [s]

        assert lower_camera_facet(scripts, follow_character_paths=players) == 1
        assert (
            "self._cam:configure({rig = self.gameObject, followCharacter = true})"
            in s.luau_source
        )
        assert ":step(dt)" in s.luau_source

        assert lower_movement_facet(players) == 1
        src = s.luau_source
        assert 'char:FindFirstChildOfClass("Humanoid")' in src
        assert "self._cam:getYawBasis():VectorToWorldSpace(Vector3.new(h, 0, -v))" in src
        assert "hum:Move(dir.Unit, false)" in src
        assert "hum:Move(Vector3.zero, false)" in src
        assert "self.gameObject:PivotTo(self.gameObject:GetPivot() + disp)" not in src
        assert "function Player:Move(dt)" in src

    def test_lowered_move_has_lazy_cam_acquire_with_follow(self) -> None:
        """Lowered Move carries the lazy _cam acquire w/ followCharacter=true."""
        s = _S(_player_src())
        players = find_player_controllers([s], _modules(cc=("Player",)))
        lower_movement_facet(players)
        src = s.luau_source
        assert "if not self._cam then" in src
        assert (
            'require(game:GetService("ReplicatedStorage")'
            ':WaitForChild("SceneCameraInput")).acquire()' in src
        )
        assert (
            "self._cam:configure({rig = self.gameObject, followCharacter = true})"
            in src
        )


class TestRealShape:
    """The dde248 shape that defeated the retired transpiled-output fingerprint:
    helper-wrapped WASD + an extra yaw term in the camera CFrame. Upstream
    identity + broadened locators must bind it END TO END."""

    def test_real_shape_binds_camera_and_movement(self) -> None:
        s = _S(_real_player_src())
        scripts = [s]
        modules = _modules(cc=("Player",))

        # Upstream identity (NOT the transpiled fingerprint) selects the player.
        players = find_player_controllers(scripts, modules)
        assert players == [s]

        # Broadened camera locator finds Rotate despite the extra yaw term.
        assert lower_camera_facet(scripts, follow_character_paths=players) == 1
        assert (
            "self._cam:configure({rig = self.gameObject, followCharacter = true})"
            in s.luau_source
        )
        assert "self._cam:step(dt)" in s.luau_source
        # The AI's camera math (extra yaw term) is gone -- routed to the service.
        assert "(basePivot - basePivot.Position)" not in s.luau_source

        # Broadened move locator finds Move despite helper-wrapped WASD.
        assert lower_movement_facet(players) == 1
        src = s.luau_source
        assert "hum:Move(dir.Unit, false)" in src
        # The _axis/_keyDown helpers (no literal KeyCode, no locomotion) are
        # untouched -- only the real move method was replaced.
        assert "function Player:_axis(posCode, negCode)" in src
        assert "function Player:_keyDown(code)" in src

    def test_helper_is_not_mistaken_for_move_method(self) -> None:
        """A pure-input helper (reads keys via a variable, no literal WASD, no
        locomotion) must never be the located move method."""
        # A script whose ONLY KeyCode literals live in _axis args inside Move,
        # plus the helpers -> exactly ONE move method (Move), helpers excluded.
        s = _S(_real_player_src())
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert lower_movement_facet(players) == 1
        # _keyDown still returns the raw IsKeyDown read (untouched).
        assert "return self.uis:IsKeyDown(code)" in s.luau_source


class TestIdempotency:
    def test_twice_call_is_noop(self) -> None:
        s = _S(_player_src())
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert lower_movement_facet(players) == 1
        once = s.luau_source
        assert lower_movement_facet(players) == 0
        assert s.luau_source == once
        assert once.count("hum:Move(dir.Unit, false)") == 1

    def test_real_shape_twice_call_is_noop(self) -> None:
        s = _S(_real_player_src())
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert lower_movement_facet(players) == 1
        once = s.luau_source
        assert lower_movement_facet(players) == 0
        assert s.luau_source == once

    def test_idempotency_is_method_scoped_not_file_global(self) -> None:
        """An unrelated ``:Move(`` ELSEWHERE must not suppress a needed first
        lowering (method-scoped idempotency)."""
        decoy_awake = textwrap.dedent("""\
            function Player:Awake()
                Player.instance = self.gameObject
                self.control = self:GetComponent("CharacterController")
                self.cam = workspace.CurrentCamera
                local basis = self.foo:getYawBasis():VectorToWorldSpace(Vector3.zero)
                self.other:Move(basis)
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + decoy_awake + "\n" + _ROTATE + "\n" + _MOVE + "\nreturn Player\n"
        )
        s = _S(src)
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert players == [s]
        assert lower_movement_facet(players) == 1
        assert "hum:Move(dir.Unit, false)" in s.luau_source
        assert "self.other:Move(basis)" in s.luau_source


class TestUpstreamIdentity:
    """Identity is the upstream ``has_character_controller`` module flag, mapped
    to the transpiled script by stem -- never the transpiled-output shape."""

    def test_no_modules_abstains(self) -> None:
        """Without the upstream signal there is no player (legacy harness)."""
        s = _S(_player_src())
        assert find_player_controllers([s]) == []
        assert find_player_controllers([s], {}) == []

    def test_unflagged_script_not_identified(self) -> None:
        """A perfect-looking FPS controller that the planner did NOT flag (no
        CharacterController on its GameObject) is not the player."""
        s = _S(_player_src())
        assert find_player_controllers([s], _modules(plain=("Player",))) == []
        assert lower_movement_facet(
            find_player_controllers([s], _modules(plain=("Player",)))
        ) == 0
        assert "hum:Move" not in s.luau_source

    def test_two_cc_modules_fail_closed(self) -> None:
        """Two distinct scripts flagged CharacterController -> ambiguous -> []
        (one-camera-per-client; never guess)."""
        a = _S(_player_src(), name="PlayerA")
        b = _S(_player_src(), name="PlayerB")
        modules = _modules(cc=("PlayerA", "PlayerB"))
        assert find_player_controllers([a, b], modules) == []
        assert lower_movement_facet(find_player_controllers([a, b], modules)) == 0
        assert "hum:Move" not in a.luau_source
        assert "hum:Move" not in b.luau_source

    def test_flagged_module_with_no_matching_script_abstains(self) -> None:
        """The CC-flagged module's stem doesn't match any transpiled script ->
        fail closed (the pipeline surfaces player_unresolved)."""
        s = _S(_player_src(), name="Player")
        # Flag a DIFFERENT stem than the only script present.
        assert find_player_controllers([s], _modules(cc=("OtherCtrl",))) == []

    def test_name_is_never_read_for_identity(self) -> None:
        """A misleadingly-named script is still identified purely by the upstream
        flag matched on its source-path stem."""
        s = _S(_player_src(), name="TotallyNotThePlayer")
        # The module stem matches the source-path stem, not the .name attr.
        players = find_player_controllers([s], _modules(cc=("TotallyNotThePlayer",)))
        assert players == [s]


class TestNonPlayerCamera:
    def test_drone_camera_lowers_without_follow_when_no_player(self) -> None:
        """A non-player camera rig (no CharacterController) still routes its look
        method to the service, but followCharacter is omitted (no player)."""
        drone_rotate = _ROTATE.replace("Player:Rotate", "Drone:Rotate")
        src = (
            "local Drone = {}\nDrone.__index = Drone\n\n"
            + drone_rotate + "\nreturn Drone\n"
        )
        s = _S(src, name="Drone")
        scripts = [s]
        players = find_player_controllers(scripts, _modules(plain=("Drone",)))
        assert players == []
        assert lower_camera_facet(scripts, follow_character_paths=players) == 1
        assert "followCharacter" not in s.luau_source
        assert "self._cam:configure({rig = self.gameObject})" in s.luau_source

    def test_nonplayer_camera_emits_followfalse_when_player_exists(self) -> None:
        """When a conversion has BOTH a player and a non-player camera, the
        drone's configure emits an EXPLICIT followCharacter = false so it can't
        inherit a stale singleton true; the player emits true."""
        drone_rotate = _ROTATE.replace("Player:Rotate", "Drone:Rotate")
        drone_src = (
            "local Drone = {}\nDrone.__index = Drone\n\n" + drone_rotate + "\nreturn Drone\n"
        )
        player = _S(_player_src(), name="Player")
        drone = _S(drone_src, name="Drone")
        scripts = [player, drone]

        players = find_player_controllers(scripts, _modules(cc=("Player",), plain=("Drone",)))
        assert players == [player]

        assert lower_camera_facet(scripts, follow_character_paths=players) == 2
        assert "self._cam:configure({rig = self.gameObject, followCharacter = true})" in player.luau_source
        assert "self._cam:configure({rig = self.gameObject, followCharacter = false})" in drone.luau_source
        assert "followCharacter = true" not in drone.luau_source

    def test_nonplayer_camera_keeps_strict_shape(self) -> None:
        """The broadened look locator is player-scoped: a NON-player script with
        a broadened-only camera shape (GetMouseDelta + extra yaw term, no strict
        pitch rebuild) is NOT lowered -- avoids false positives like a cutscene
        camera in some other controller's Awake."""
        broad_only = _REAL_ROTATE.replace("Player:Rotate", "Drone:Look")
        src = (
            "local Drone = {}\nDrone.__index = Drone\n\n" + broad_only + "\nreturn Drone\n"
        )
        s = _S(src, name="Drone")
        before = s.luau_source
        # No player; drone uses the STRICT locator, which the broadened-only
        # shape does not match -> untouched.
        assert lower_camera_facet([s], follow_character_paths=[]) == 0
        assert s.luau_source == before


class TestMoveLocatorFailClosed:
    def test_two_move_methods_fail_closed(self) -> None:
        """An identified player with TWO colon-methods each reading >=3 WASD keys
        + locomotion is ambiguous -> the move locator abstains (0 lowered)."""
        second = textwrap.dedent("""\
            function Player:MoveAlt(dt)
                local UIS = game:GetService("UserInputService")
                local h = 0
                if UIS:IsKeyDown(Enum.KeyCode.D) then h = h + 1 end
                if UIS:IsKeyDown(Enum.KeyCode.A) then h = h - 1 end
                local v = 0
                if UIS:IsKeyDown(Enum.KeyCode.W) then v = v + 1 end
                if UIS:IsKeyDown(Enum.KeyCode.S) then v = v - 1 end
                self.gameObject:PivotTo(self.gameObject:GetPivot() + Vector3.new(h, 0, v))
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _ROTATE + "\n" + _MOVE + "\n" + second
            + "\nreturn Player\n"
        )
        s = _S(src)
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert players == [s]
        assert lower_movement_facet(players) == 0
        # Even force-passed, lowering refuses on the ambiguous shape.
        assert lower_movement_facet([s]) == 0

    def test_wasd_only_in_string_not_located(self) -> None:
        """An identified player whose only WASD reads are inside a string literal
        has no locatable move method (the locator scans lexer-blanked source)."""
        fake_move = textwrap.dedent("""\
            function Player:Move(dt)
                local doc = "Enum.KeyCode.W Enum.KeyCode.A Enum.KeyCode.S Enum.KeyCode.D :Move("
                return doc
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _ROTATE + "\n" + fake_move + "\nreturn Player\n"
        )
        s = _S(src)
        before = s.luau_source
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert players == [s]
        assert lower_movement_facet(players) == 0
        assert s.luau_source == before

    def test_pure_input_helper_not_located(self) -> None:
        """A method that reads >=3 WASD keys but has NO locomotion side-effect (a
        pure input reader) is not the move method -- the locomotion gate excludes
        it, so a player with only such a method abstains."""
        reader = textwrap.dedent("""\
            function Player:ReadMoveInput()
                local h = 0
                if self.uis:IsKeyDown(Enum.KeyCode.D) then h = h + 1 end
                if self.uis:IsKeyDown(Enum.KeyCode.A) then h = h - 1 end
                local v = 0
                if self.uis:IsKeyDown(Enum.KeyCode.W) then v = v + 1 end
                if self.uis:IsKeyDown(Enum.KeyCode.S) then v = v - 1 end
                return Vector3.new(h, 0, v)
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _ROTATE + "\n" + reader + "\nreturn Player\n"
        )
        s = _S(src)
        before = s.luau_source
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert players == [s]
        # No locomotion side-effect anywhere -> no move method located.
        assert lower_movement_facet(players) == 0
        assert s.luau_source == before


class TestLookLocatorAmbiguity:
    """The broadened look locator must not first-match-win onto the wrong method
    inside the player script (codex P1.2)."""

    def test_ads_method_before_rotate_binds_rotate(self) -> None:
        """An aim/zoom method that reads GetMouseDelta + owns the camera but does
        NOT write a camera orientation (no CFrame.Angles) must NOT be mistaken
        for the look method -- the real Rotate (which writes CFrame.Angles) is
        bound instead, even though the aim method is declared first."""
        prime = textwrap.dedent("""\
            function Player:PrimeMouse()
                local d = self.uis:GetMouseDelta()
                local cam = workspace.CurrentCamera
                if cam then cam.CameraType = Enum.CameraType.Scriptable end
                self.fov = 70
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + prime + "\n" + _REAL_ROTATE + "\nreturn Player\n"
        )
        s = _S(src)
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert lower_camera_facet([s], follow_character_paths=players) == 1
        out = s.luau_source
        # PrimeMouse untouched (still reads the mouse), Rotate routed to step.
        assert "self.fov = 70" in out and "function Player:PrimeMouse()" in out
        assert "self._cam:step(dt)" in out
        assert "(basePivot - basePivot.Position)" not in out  # Rotate replaced

    def test_two_broad_look_methods_fail_closed(self) -> None:
        """A player with TWO genuine mouse-look methods (each GetMouseDelta +
        camera-owner + CFrame.Angles) is ambiguous -> the look locator abstains
        rather than first-match-win onto one."""
        lean = _REAL_ROTATE.replace("Player:Rotate", "Player:Lean")
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _REAL_ROTATE + "\n" + lean + "\nreturn Player\n"
        )
        s = _S(src)
        before = s.luau_source
        players = find_player_controllers([s], _modules(cc=("Player",)))
        # Two broad matches, no strict winner -> abstain -> nothing lowered.
        assert lower_camera_facet([s], follow_character_paths=players) == 0
        assert s.luau_source == before

    def test_strict_preferred_over_broad(self) -> None:
        """When a player has the canonical strict look method AND a broad-only
        aim method, the strict one is bound (prefer-strict)."""
        aim = _REAL_ROTATE.replace("Player:Rotate", "Player:Aim")
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _ROTATE + "\n" + aim + "\nreturn Player\n"
        )
        s = _S(src)
        players = find_player_controllers([s], _modules(cc=("Player",)))
        assert lower_camera_facet([s], follow_character_paths=players) == 1
        # The strict Rotate was replaced; the broad-only Aim still has its mouse read.
        assert "self._cam:step(dt)" in s.luau_source
        assert "(basePivot - basePivot.Position)" in s.luau_source  # Aim untouched


class TestMoveLocatorHelperGuard:
    def test_floormaterial_reader_helper_not_move_method(self) -> None:
        """A helper that reads WASD + FloorMaterial but only RETURNS a vector
        (no Humanoid:Move / Jump / PivotTo drive) must not be taken as the move
        method; with the real Move's WASD factored into the helper, neither
        method qualifies -> abstain (codex P1.3: FloorMaterial is a read)."""
        intent = textwrap.dedent("""\
            function Player:ReadMovementIntent()
                local grounded = self.control and self.control.FloorMaterial ~= Enum.Material.Air
                local h = self:_axis(Enum.KeyCode.D, Enum.KeyCode.A)
                local v = self:_axis(Enum.KeyCode.W, Enum.KeyCode.S)
                return Vector3.new(h, 0, -v)
            end
        """)
        move = textwrap.dedent("""\
            function Player:Move(dt)
                local dir = self:ReadMovementIntent()
                self.gameObject:PivotTo(self.gameObject:GetPivot() + dir)
            end
        """)
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _ROTATE + "\n" + intent + "\n" + move + "\nreturn Player\n"
        )
        s = _S(src)
        before = s.luau_source
        players = find_player_controllers([s], _modules(cc=("Player",)))
        # The reader helper (no drive) and Move (no WASD literals) both fail the
        # combined gate -> nothing lowered (surfaced as player_move_unbound).
        assert lower_movement_facet(players) == 0
        assert s.luau_source == before


# --- Pipeline-invocation integration ---------------------------------------


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


class TestPipelineInvocation:
    """Drives the REAL ``contract_pipeline.transpile_with_contract`` so a future
    edit deleting the identity -> camera -> movement wiring FAILS here."""

    def _run(self, luau_source: str):
        from converter import contract_pipeline

        player_path = Path("/proj/Assets/Player.cs")
        infos = [_PInfo(player_path, "Player")]
        scene_runtime = {
            "modules": {
                "guid-player": _row("Player", has_cc=True),
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        player_script = TranspiledScript(
            source_path=str(player_path),
            output_filename="Player.luau",
            csharp_source="",
            luau_source=luau_source,
            strategy="ai",
            confidence=1.0,
            script_type="ModuleScript",
        )
        stub_result = TranspilationResult()
        stub_result.total_transpiled = 1
        stub_result.scripts.append(player_script)
        with patch(
            "converter.contract_pipeline.transpile_scripts",
            return_value=stub_result,
        ) as mock_transpile:
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        assert mock_transpile.called
        return result

    def test_generic_pipeline_lowers_player_movement_and_follow(self) -> None:
        result = self._run(_player_src())
        lowered_src = result.transpilation.scripts[0].luau_source
        assert "hum:Move(dir.Unit, false)" in lowered_src
        assert (
            "self._cam:getYawBasis():VectorToWorldSpace(Vector3.new(h, 0, -v))"
            in lowered_src
        )
        assert (
            "self.gameObject:PivotTo(self.gameObject:GetPivot() + disp)"
            not in lowered_src
        )
        assert (
            "self._cam:configure({rig = self.gameObject, followCharacter = true})"
            in lowered_src
        )
        assert ":step(dt)" in lowered_src
        # A cleanly-bound player surfaces no player-binding fail-closed rows.
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_move_unbound" not in kinds
        assert "player_look_unbound" not in kinds

    def test_generic_pipeline_binds_the_real_dde248_shape(self) -> None:
        """End-to-end through the pipeline on the shape that broke in Studio."""
        result = self._run(_real_player_src())
        lowered_src = result.transpilation.scripts[0].luau_source
        assert "hum:Move(dir.Unit, false)" in lowered_src
        assert "self._cam:step(dt)" in lowered_src
        assert "(basePivot - basePivot.Position)" not in lowered_src
        kinds = {fc.kind for fc in result.fail_closed}
        assert not (kinds & {
            "player_ambiguous", "player_unresolved",
            "player_move_unbound", "player_look_unbound",
        })

    def test_pipeline_surfaces_player_look_unbound(self) -> None:
        """Player identified, movement bound, but the look method is ambiguous
        (two broad look methods) -> the pipeline surfaces player_look_unbound
        instead of silently shipping an unbound camera."""
        lean = _REAL_ROTATE.replace("Player:Rotate", "Player:Lean")
        src = (
            "local Player = {}\nPlayer.__index = Player\n\n"
            + _AWAKE + "\n" + _REAL_ROTATE + "\n" + lean + "\n" + _REAL_MOVE
            + "\n" + _HELPERS + "\nreturn Player\n"
        )
        result = self._run(src)
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_look_unbound" in kinds
        assert "player_move_unbound" not in kinds  # movement still bound

    def test_pipeline_surfaces_player_signal_absent(self) -> None:
        """A scene_runtime artifact that predates the upstream signal (no
        has_character_controller key on any module) surfaces player_signal_absent
        rather than silently skipping player binding."""
        from converter import contract_pipeline

        player_path = Path("/proj/Assets/Player.cs")
        infos = [_PInfo(player_path, "Player")]
        # Module row WITHOUT has_character_controller AND without
        # is_component_class (an artifact old enough to predate both) -- only
        # runtime_bearing (present since PR1) is left to trip the guard.
        stale_row = {
            "stem": "Player", "class_name": "Player", "runtime_bearing": True,
            "character_attached": False, "is_loader": False,
        }
        scene_runtime = {
            "modules": {"guid-player": stale_row},
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        ps = TranspiledScript(
            source_path=str(player_path), output_filename="Player.luau",
            csharp_source="", luau_source=_player_src(), strategy="ai",
            confidence=1.0, script_type="ModuleScript",
        )
        stub = TranspilationResult()
        stub.total_transpiled = 1
        stub.scripts.append(ps)
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_signal_absent" in kinds

    def test_pipeline_surfaces_player_ambiguous(self) -> None:
        """>1 CharacterController-bearing script -> a player_ambiguous row, and
        nothing bound."""
        from converter import contract_pipeline

        infos = [
            _PInfo(Path("/proj/Assets/PlayerA.cs"), "PlayerA"),
            _PInfo(Path("/proj/Assets/PlayerB.cs"), "PlayerB"),
        ]
        scene_runtime = {
            "modules": {
                "guid-a": _row("PlayerA", has_cc=True),
                "guid-b": _row("PlayerB", has_cc=True),
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        scripts = []
        for stem in ("PlayerA", "PlayerB"):
            scripts.append(TranspiledScript(
                source_path=f"/proj/Assets/{stem}.cs",
                output_filename=f"{stem}.luau",
                csharp_source="",
                luau_source=_player_src().replace("Player", stem),
                strategy="ai", confidence=1.0, script_type="ModuleScript",
            ))
        stub = TranspilationResult()
        stub.total_transpiled = 2
        stub.scripts.extend(scripts)
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_ambiguous" in kinds
        for sc in result.transpilation.scripts:
            assert "hum:Move" not in sc.luau_source
