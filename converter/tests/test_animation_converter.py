"""
Tests for the animation converter module.

Tests cover:
  - .anim file parsing (position, rotation, euler, scale curves)
  - .controller file parsing (states, transitions, parameters)
  - Keyframe simplification
  - Luau TweenService code generation
  - Project-level animation discovery
  - Integration with the pipeline
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from converter.animation_converter import (
    AnimClip,
    AnimCurve,
    AnimKeyframe,
    AnimParameter,
    AnimatorController,
    parse_anim_file,
    parse_controller_file,
    simplify_keyframes,
    generate_tween_script,
    discover_animations,
    convert_animations,
    _quat_to_euler_degrees,
)


# ---------------------------------------------------------------------------
# Fixtures -- minimal .anim and .controller YAML content
# ---------------------------------------------------------------------------

MINIMAL_ANIM_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_ObjectHideFlags: 0
  m_Name: test_open
  m_PositionCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 0, y: 0, z: 0}
        inSlope: {x: 0, y: 0, z: 0}
        outSlope: {x: 0, y: 0, z: 0}
      - serializedVersion: 3
        time: 1
        value: {x: 0, y: 4, z: 0}
        inSlope: {x: 0, y: 0, z: 0}
        outSlope: {x: 0, y: 0, z: 0}
      m_PreInfinity: 2
      m_PostInfinity: 2
    path: ''
  m_RotationCurves: []
  m_EulerCurves: []
  m_ScaleCurves: []
  m_SampleRate: 60
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StartTime: 0
    m_StopTime: 1
    m_LoopTime: 0
"""

ROTATION_ANIM_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_ObjectHideFlags: 0
  m_Name: test_rotate
  m_PositionCurves: []
  m_RotationCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 0, y: 0, z: 0, w: 1}
      - serializedVersion: 3
        time: 2
        value: {x: 0, y: 0.7071068, z: 0, w: 0.7071068}
      m_PreInfinity: 2
      m_PostInfinity: 2
    path: ''
  m_EulerCurves: []
  m_ScaleCurves: []
  m_SampleRate: 60
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StartTime: 0
    m_StopTime: 2
    m_LoopTime: 1
"""

SCALE_ANIM_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_ObjectHideFlags: 0
  m_Name: test_scale
  m_PositionCurves: []
  m_RotationCurves: []
  m_EulerCurves: []
  m_ScaleCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 1, y: 1, z: 1}
      - serializedVersion: 3
        time: 0.5
        value: {x: 2, y: 2, z: 2}
      m_PreInfinity: 2
      m_PostInfinity: 2
    path: ''
  m_SampleRate: 60
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StartTime: 0
    m_StopTime: 0.5
    m_LoopTime: 0
"""

CHILD_PATH_ANIM_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_ObjectHideFlags: 0
  m_Name: test_child
  m_PositionCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 0, y: 0, z: 2}
      - serializedVersion: 3
        time: 1
        value: {x: 0, y: 0, z: -2}
      m_PreInfinity: 2
      m_PostInfinity: 2
    path: Base10/Box1
  m_RotationCurves: []
  m_EulerCurves: []
  m_ScaleCurves: []
  m_SampleRate: 60
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StartTime: 0
    m_StopTime: 1
    m_LoopTime: 0
"""

MINIMAL_CONTROLLER_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!91 &9100000
AnimatorController:
  m_ObjectHideFlags: 0
  m_Name: TestController
  m_AnimatorParameters:
  - m_Name: open
    m_Type: 4
    m_DefaultFloat: 0
    m_DefaultInt: 0
    m_DefaultBool: 0
  m_AnimatorLayers:
  - serializedVersion: 5
    m_Name: Base Layer
    m_StateMachine: {fileID: 110747030}
--- !u!1101 &110115666
AnimatorStateTransition:
  m_ObjectHideFlags: 1
  m_Name: Opened
  m_Conditions:
  - m_ConditionMode: 1
    m_ConditionEvent: open
    m_EventTreshold: 0
  m_DstState: {fileID: 110220728}
  serializedVersion: 3
  m_TransitionDuration: 0.1
  m_HasExitTime: 0
  m_ExitTime: 0.9
--- !u!1102 &110218898
AnimatorState:
  serializedVersion: 6
  m_ObjectHideFlags: 1
  m_Name: Idle
  m_Speed: 1
  m_Transitions:
  - {fileID: 110115666}
  m_Motion: {fileID: 0}
--- !u!1102 &110220728
AnimatorState:
  serializedVersion: 6
  m_ObjectHideFlags: 1
  m_Name: open
  m_Speed: 1
  m_Transitions: []
  m_Motion: {fileID: 7400000, guid: 788698ac0388d224ca96f4fc598e0f1d, type: 2}
--- !u!1107 &110747030
AnimatorStateMachine:
  serializedVersion: 6
  m_ObjectHideFlags: 1
  m_Name: Base Layer
  m_ChildStates:
  - serializedVersion: 1
    m_State: {fileID: 110218898}
  - serializedVersion: 1
    m_State: {fileID: 110220728}
  m_DefaultState: {fileID: 110218898}
"""


# ---------------------------------------------------------------------------
# Test .anim parsing
# ---------------------------------------------------------------------------

class TestAnimParsing:
    """Tests for parsing Unity .anim files."""

    def test_parse_position_anim(self, tmp_path: Path) -> None:
        """Parse a simple position animation with 2 keyframes."""
        anim_file = tmp_path / "open.anim"
        anim_file.write_text(MINIMAL_ANIM_YAML, encoding="utf-8")

        clip = parse_anim_file(anim_file)

        assert clip is not None
        assert clip.name == "test_open"
        assert clip.duration == pytest.approx(1.0)
        assert clip.loop is False
        assert clip.sample_rate == 60.0
        assert len(clip.curves) == 1

        curve = clip.curves[0]
        assert curve.property_type == "position"
        assert curve.path == ""
        assert len(curve.keyframes) == 2

        kf0 = curve.keyframes[0]
        assert kf0.time == pytest.approx(0.0)
        assert kf0.value == pytest.approx((0.0, 0.0, 0.0))

        kf1 = curve.keyframes[1]
        assert kf1.time == pytest.approx(1.0)
        assert kf1.value == pytest.approx((0.0, 4.0, 0.0))

    def test_parse_rotation_anim(self, tmp_path: Path) -> None:
        """Parse a quaternion rotation animation."""
        anim_file = tmp_path / "rotate.anim"
        anim_file.write_text(ROTATION_ANIM_YAML, encoding="utf-8")

        clip = parse_anim_file(anim_file)

        assert clip is not None
        assert clip.name == "test_rotate"
        assert clip.duration == pytest.approx(2.0)
        assert clip.loop is True
        assert len(clip.curves) == 1

        curve = clip.curves[0]
        assert curve.property_type == "rotation"
        assert len(curve.keyframes) == 2

        kf0 = curve.keyframes[0]
        assert kf0.value == pytest.approx((0.0, 0.0, 0.0, 1.0))

        kf1 = curve.keyframes[1]
        assert kf1.value == pytest.approx((0.0, 0.7071068, 0.0, 0.7071068), abs=1e-5)

    def test_parse_scale_anim(self, tmp_path: Path) -> None:
        """Parse a scale animation."""
        anim_file = tmp_path / "scale.anim"
        anim_file.write_text(SCALE_ANIM_YAML, encoding="utf-8")

        clip = parse_anim_file(anim_file)

        assert clip is not None
        assert clip.name == "test_scale"
        assert clip.duration == pytest.approx(0.5)
        assert len(clip.curves) == 1
        assert clip.curves[0].property_type == "scale"
        assert len(clip.curves[0].keyframes) == 2

    def test_parse_child_path_anim(self, tmp_path: Path) -> None:
        """Parse an animation that targets a child object by path."""
        anim_file = tmp_path / "child.anim"
        anim_file.write_text(CHILD_PATH_ANIM_YAML, encoding="utf-8")

        clip = parse_anim_file(anim_file)

        assert clip is not None
        assert clip.name == "test_child"
        assert len(clip.curves) == 1
        assert clip.curves[0].path == "Base10/Box1"

    def test_parse_nonexistent_file(self, tmp_path: Path) -> None:
        """Return None for a nonexistent file."""
        clip = parse_anim_file(tmp_path / "nonexistent.anim")
        assert clip is None

    def test_parse_clip_with_non_zero_start_time_rebases_keyframes(
        self, tmp_path: Path,
    ) -> None:
        """Unity authors can crop a clip to a sub-window of its source
        timeline via m_StartTime / m_StopTime. Keyframes outside the
        window must be dropped and the survivors rebased to 0 — otherwise
        the runtime tweens over the wrong absolute range and never plays
        the cropped portion correctly.
        """
        yaml_content = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_ObjectHideFlags: 0
  m_Name: cropped_window
  m_PositionCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 0, y: 0, z: 0}
      - serializedVersion: 3
        time: 1
        value: {x: 1, y: 0, z: 0}
      - serializedVersion: 3
        time: 2.5
        value: {x: 2, y: 0, z: 0}
      - serializedVersion: 3
        time: 4
        value: {x: 3, y: 0, z: 0}
      - serializedVersion: 3
        time: 5.5
        value: {x: 4, y: 0, z: 0}
      m_PreInfinity: 2
      m_PostInfinity: 2
    path: ''
  m_RotationCurves: []
  m_EulerCurves: []
  m_ScaleCurves: []
  m_SampleRate: 60
  m_Events:
  - time: 1.0
    functionName: BeforeWindow
  - time: 3.0
    functionName: InsideWindow
  - time: 6.0
    functionName: AfterWindow
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StartTime: 2.0
    m_StopTime: 5.0
    m_LoopTime: 0
"""
        anim_file = tmp_path / "cropped.anim"
        anim_file.write_text(yaml_content, encoding="utf-8")

        clip = parse_anim_file(anim_file)
        assert clip is not None
        assert clip.duration == pytest.approx(3.0)

        assert len(clip.curves) == 1
        curve = clip.curves[0]
        # Only the t=2.5 and t=4.0 keyframes fall inside [2.0, 5.0].
        kf_times = [kf.time for kf in curve.keyframes]
        assert kf_times == pytest.approx([0.5, 2.0]), (
            f"Keyframes outside window must drop and survivors rebase to 0; "
            f"got {kf_times}"
        )
        # Every surviving time must fit the duration window [0, 3].
        for kf in curve.keyframes:
            assert 0.0 <= kf.time <= clip.duration

        # Events: t=1.0 is before window, t=3.0 inside (rebases to 1.0),
        # t=6.0 after.
        evt_names = [e.function_name for e in clip.events]
        evt_times = [e.time for e in clip.events]
        assert evt_names == ["InsideWindow"]
        assert evt_times == pytest.approx([1.0])

    def test_parse_empty_curves(self, tmp_path: Path) -> None:
        """Handle anim files with no curves gracefully."""
        yaml_content = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!74 &7400000
AnimationClip:
  m_Name: empty
  m_PositionCurves: []
  m_RotationCurves: []
  m_EulerCurves: []
  m_ScaleCurves: []
  m_SampleRate: 60
  m_AnimationClipSettings:
    m_StartTime: 0
    m_StopTime: 1
    m_LoopTime: 0
"""
        anim_file = tmp_path / "empty.anim"
        anim_file.write_text(yaml_content, encoding="utf-8")

        clip = parse_anim_file(anim_file)
        assert clip is not None
        assert clip.name == "empty"
        assert len(clip.curves) == 0


class TestBinaryAnimParsing:
    """Binary .anim files route through UnityPy. We mock the UnityPy
    surface to avoid needing a binary fixture in-tree -- the seam under
    test is whether the typetree dict is fed correctly to _parse_clip_body."""

    def _binary_anim_file(self, tmp_path: Path) -> Path:
        """Create a non-YAML byte file so is_text_yaml() returns False."""
        f = tmp_path / "binary.anim"
        f.write_bytes(b"\x00\x01\x02UnityFS\x00not-yaml")
        return f

    def _make_fake_env(self, typetree: dict[str, Any]):
        """Build a stand-in for UnityPy's Environment with a single
        AnimationClip object whose read_typetree() returns typetree."""
        class _FakeType:
            name = "AnimationClip"

        class _FakeObj:
            type = _FakeType()
            def read_typetree(self) -> dict[str, Any]:
                return typetree

        class _FakeEnv:
            objects = [_FakeObj()]

        return _FakeEnv()

    def test_binary_anim_lowers_to_clip_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binary anim with a position curve produces an AnimClip identical
        in shape to the YAML path."""
        import UnityPy

        typetree: dict[str, Any] = {
            "m_Name": "binary_open",
            "m_SampleRate": 60.0,
            "m_AnimationClipSettings": {
                "m_StartTime": 0.0,
                "m_StopTime": 1.0,
                "m_LoopTime": 0,
            },
            "m_PositionCurves": [{
                "path": "",
                "curve": {
                    "m_Curve": [
                        {"time": 0.0, "value": {"x": 0.0, "y": 0.0, "z": 0.0}},
                        {"time": 1.0, "value": {"x": 0.0, "y": 4.0, "z": 0.0}},
                    ],
                },
            }],
            "m_RotationCurves": [],
            "m_EulerCurves": [],
            "m_ScaleCurves": [],
            "m_FloatCurves": [],
            "m_Events": [],
        }
        fake_env = self._make_fake_env(typetree)
        monkeypatch.setattr(UnityPy, "load", lambda _path: fake_env)

        clip = parse_anim_file(self._binary_anim_file(tmp_path))
        assert clip is not None
        assert clip.name == "binary_open"
        assert clip.duration == pytest.approx(1.0)
        assert clip.loop is False
        assert clip.sample_rate == 60.0
        assert len(clip.curves) == 1

        curve = clip.curves[0]
        assert curve.property_type == "position"
        assert len(curve.keyframes) == 2
        assert curve.keyframes[0].value == pytest.approx((0.0, 0.0, 0.0))
        assert curve.keyframes[1].value == pytest.approx((0.0, 4.0, 0.0))

    def test_binary_anim_no_clip_object_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A binary file with no AnimationClip yields None (skipped)."""
        import UnityPy

        class _FakeType:
            name = "GameObject"

        class _FakeObj:
            type = _FakeType()
            def read_typetree(self) -> dict[str, Any]:
                return {}

        class _FakeEnv:
            objects = [_FakeObj()]

        monkeypatch.setattr(UnityPy, "load", lambda _path: _FakeEnv())
        clip = parse_anim_file(self._binary_anim_file(tmp_path))
        assert clip is None

    def test_binary_anim_unitypy_load_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If UnityPy raises on load, the function returns None instead of
        propagating -- a malformed file shouldn't crash the pipeline."""
        import UnityPy

        def _boom(_path: str):
            raise RuntimeError("not a real serialized file")

        monkeypatch.setattr(UnityPy, "load", _boom)
        clip = parse_anim_file(self._binary_anim_file(tmp_path))
        assert clip is None

    def test_binary_anim_typetree_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If read_typetree raises, the function returns None."""
        import UnityPy

        class _FakeType:
            name = "AnimationClip"

        class _FakeObj:
            type = _FakeType()
            def read_typetree(self) -> dict[str, Any]:
                raise RuntimeError("typetree corrupt")

        class _FakeEnv:
            objects = [_FakeObj()]

        monkeypatch.setattr(UnityPy, "load", lambda _path: _FakeEnv())
        clip = parse_anim_file(self._binary_anim_file(tmp_path))
        assert clip is None


# ---------------------------------------------------------------------------
# Test .controller parsing
# ---------------------------------------------------------------------------

class TestControllerParsing:
    """Tests for parsing Unity .controller files."""

    def test_parse_controller(self, tmp_path: Path) -> None:
        """Parse a controller with parameters, states, and transitions."""
        ctrl_file = tmp_path / "door.controller"
        ctrl_file.write_text(MINIMAL_CONTROLLER_YAML, encoding="utf-8")

        ctrl = parse_controller_file(ctrl_file)

        assert ctrl is not None
        assert ctrl.name == "TestController"

        # Parameters
        assert len(ctrl.parameters) == 1
        assert ctrl.parameters[0].name == "open"
        assert ctrl.parameters[0].param_type == 4  # Bool

        # States
        assert len(ctrl.states) == 2
        state_names = {s.name for s in ctrl.states}
        assert "Idle" in state_names
        assert "open" in state_names

        # Default state
        assert ctrl.default_state_file_id == "110218898"

        # Transitions on Idle state
        idle_state = next(s for s in ctrl.states if s.name == "Idle")
        assert len(idle_state.transitions) == 1

        trans = idle_state.transitions[0]
        assert trans.name == "Opened"
        assert trans.dst_state_file_id == "110220728"
        assert len(trans.conditions) == 1
        assert trans.conditions[0].parameter == "open"
        assert trans.conditions[0].mode == 1  # If (true)

        # Open state has the clip GUID
        open_state = next(s for s in ctrl.states if s.name == "open")
        assert open_state.clip_guid == "788698ac0388d224ca96f4fc598e0f1d"

    def test_parse_controller_nonexistent(self, tmp_path: Path) -> None:
        """Return None for a nonexistent controller file."""
        ctrl = parse_controller_file(tmp_path / "nonexistent.controller")
        assert ctrl is None


class TestBinaryControllerParsing:
    """Binary .controller files route through UnityPy. We mock the UnityPy
    surface to avoid needing a binary fixture in-tree -- the seam under
    test is whether the typetree graph (AnimatorController + AnimatorState +
    AnimatorStateMachine + AnimatorStateTransition + BlendTree) lowers into
    the same docs_by_fid shape the YAML path produces, with PPtrs
    translated into fileID/guid keys."""

    def _binary_controller_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "binary.controller"
        f.write_bytes(b"\x00\x01\x02UnityFS\x00not-yaml")
        return f

    def _make_obj(self, type_name: str, path_id: int, body: dict[str, Any]) -> Any:
        class _FakeType:
            name = type_name

        class _FakeObj:
            type = _FakeType()
            def __init__(self, pid: int, b: dict[str, Any]) -> None:
                self.path_id = pid
                self._body = b
                self.assets_file = None  # set by _make_env
            def read_typetree(self) -> dict[str, Any]:
                return self._body

        return _FakeObj(path_id, body)

    def _make_env(
        self,
        objs: list[Any],
        externals: list[Any] | None = None,
    ) -> Any:
        class _FakeAssetsFile:
            def __init__(self, ext: list[Any]) -> None:
                self.externals = ext

        af = _FakeAssetsFile(list(externals) if externals else [])
        for obj in objs:
            obj.assets_file = af

        class _FakeEnv:
            objects = objs

        return _FakeEnv()

    def _make_external(self, guid: str) -> Any:
        class _FakeExt:
            def __init__(self, g: str) -> None:
                self.guid = g
        return _FakeExt(guid)

    def test_binary_controller_lowers_to_yaml_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binary controller with state machine + states + transitions
        produces the same AnimatorController structure the YAML path
        builds. PPtrs (m_FileID/m_PathID) are translated so the local
        state-machine/state/transition lookups all resolve."""
        import UnityPy

        # AnimatorController -> StateMachine (path 200) -> State Idle (300)
        #   transitions to State Walk (400) on parameter "Speed" > 0.1
        controller_body: dict[str, Any] = {
            "m_Name": "Hero",
            "m_AnimatorParameters": [{
                "m_Name": "Speed",
                "m_Type": 1,  # Float
                "m_DefaultFloat": 0.0,
                "m_DefaultInt": 0,
                "m_DefaultBool": 0,
            }],
            "m_AnimatorLayers": [{
                "m_Name": "Base Layer",
                "m_StateMachine": {"m_FileID": 0, "m_PathID": 200},
            }],
        }
        sm_body: dict[str, Any] = {
            "m_DefaultState": {"m_FileID": 0, "m_PathID": 300},
        }
        idle_body: dict[str, Any] = {
            "m_Name": "Idle",
            "m_Speed": 1.0,
            "m_Motion": {
                "m_FileID": 1,  # external — clip in another .anim
                "m_PathID": 7400000,
            },
            "m_Transitions": [
                {"m_FileID": 0, "m_PathID": 500},
            ],
        }
        walk_body: dict[str, Any] = {
            "m_Name": "Walk",
            "m_Speed": 1.0,
            "m_Motion": {"m_FileID": 0, "m_PathID": 0},
            "m_Transitions": [],
        }
        trans_body: dict[str, Any] = {
            "m_Name": "ToWalk",
            "m_DstState": {"m_FileID": 0, "m_PathID": 400},
            "m_HasExitTime": 0,
            "m_ExitTime": 0.0,
            "m_TransitionDuration": 0.25,
            "m_Conditions": [{
                "m_ConditionEvent": "Speed",
                "m_ConditionMode": 3,  # Greater
                "m_EventTreshold": 0.1,
            }],
        }

        objs = [
            self._make_obj("AnimatorController", 100, controller_body),
            self._make_obj("AnimatorStateMachine", 200, sm_body),
            self._make_obj("AnimatorState", 300, idle_body),
            self._make_obj("AnimatorState", 400, walk_body),
            self._make_obj("AnimatorStateTransition", 500, trans_body),
        ]
        externals = [self._make_external("a" * 32)]
        fake_env = self._make_env(objs, externals)
        monkeypatch.setattr(UnityPy, "load", lambda _path: fake_env)

        ctrl = parse_controller_file(self._binary_controller_file(tmp_path))
        assert ctrl is not None
        assert ctrl.name == "Hero"
        assert len(ctrl.parameters) == 1
        assert ctrl.parameters[0].name == "Speed"
        assert ctrl.parameters[0].param_type == 1
        assert ctrl.default_state_file_id == "300"

        # Both states preserved with correct names + speeds
        names = {s.name for s in ctrl.states}
        assert names == {"Idle", "Walk"}

        idle = next(s for s in ctrl.states if s.name == "Idle")
        # External clip GUID resolved from m_FileID=1 → externals[0]
        assert idle.clip_guid == "a" * 32
        assert len(idle.transitions) == 1
        t = idle.transitions[0]
        assert t.dst_state_file_id == "400"
        assert t.transition_duration == pytest.approx(0.25)
        assert t.conditions[0].parameter == "Speed"
        assert t.conditions[0].mode == 3
        assert t.conditions[0].threshold == pytest.approx(0.1)

    def test_binary_controller_resolves_local_blend_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A state pointing at a local BlendTree (m_FileID=0, m_PathID=<bt>)
        resolves through docs_by_fid the same way the YAML walker does,
        and the 1D blend tree gets recorded on the controller."""
        import UnityPy

        controller_body: dict[str, Any] = {
            "m_Name": "Mover",
            "m_AnimatorParameters": [],
            "m_AnimatorLayers": [{
                "m_Name": "Base Layer",
                "m_StateMachine": {"m_FileID": 0, "m_PathID": 200},
            }],
        }
        sm_body: dict[str, Any] = {
            "m_DefaultState": {"m_FileID": 0, "m_PathID": 300},
        }
        state_body: dict[str, Any] = {
            "m_Name": "Move",
            "m_Speed": 1.0,
            "m_Motion": {"m_FileID": 0, "m_PathID": 600},  # → BlendTree
            "m_Transitions": [],
        }
        bt_body: dict[str, Any] = {
            "m_Name": "MoveTree",
            "m_BlendType": 0,  # 1D
            "m_BlendParameter": "Speed",
            "m_Childs": [
                {
                    "m_Threshold": 0.0,
                    # External anim ref — clip GUID resolved via externals
                    "m_Motion": {"m_FileID": 1, "m_PathID": 7400000},
                },
                {
                    "m_Threshold": 1.0,
                    "m_Motion": {"m_FileID": 2, "m_PathID": 7400000},
                },
            ],
        }

        objs = [
            self._make_obj("AnimatorController", 100, controller_body),
            self._make_obj("AnimatorStateMachine", 200, sm_body),
            self._make_obj("AnimatorState", 300, state_body),
            self._make_obj("BlendTree", 600, bt_body),
        ]
        externals = [
            self._make_external("b" * 32),
            self._make_external("c" * 32),
        ]
        fake_env = self._make_env(objs, externals)
        monkeypatch.setattr(UnityPy, "load", lambda _path: fake_env)

        ctrl = parse_controller_file(self._binary_controller_file(tmp_path))
        assert ctrl is not None
        assert "MoveTree" in ctrl.blend_trees
        bt = ctrl.blend_trees["MoveTree"]
        assert bt.param == "Speed"
        assert len(bt.entries) == 2
        assert {e.clip_guid for e in bt.entries} == {"b" * 32, "c" * 32}

        # State carries the blend tree name and a fallback clip GUID.
        move = next(s for s in ctrl.states if s.name == "Move")
        assert move.blend_tree_name == "MoveTree"
        assert move.clip_guid in ("b" * 32, "c" * 32)

    def test_binary_controller_unitypy_load_failure_records_unconverted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When UnityPy.load raises, return None and emit an UNCONVERTED
        entry — the pipeline must still surface the unparsed file rather
        than silently dropping it."""
        import UnityPy

        def _boom(_path: str) -> None:
            raise RuntimeError("not a real serialized file")

        monkeypatch.setattr(UnityPy, "load", _boom)
        out: list[dict[str, str]] = []
        ctrl = parse_controller_file(
            self._binary_controller_file(tmp_path),
            unconverted_out=out,
        )
        assert ctrl is None
        assert len(out) == 1
        assert out[0]["category"] == "animator_controller"
        assert "binary" in out[0]["reason"].lower()

    def test_binary_controller_no_animator_object_records_unconverted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If UnityPy loads but finds no AnimatorController, emit an
        UNCONVERTED entry with that specific reason."""
        import UnityPy

        objs = [self._make_obj("GameObject", 1, {"m_Name": "junk"})]
        fake_env = self._make_env(objs)
        monkeypatch.setattr(UnityPy, "load", lambda _path: fake_env)

        out: list[dict[str, str]] = []
        ctrl = parse_controller_file(
            self._binary_controller_file(tmp_path),
            unconverted_out=out,
        )
        assert ctrl is None
        assert len(out) == 1
        assert "no AnimatorController" in out[0]["reason"]


# ---------------------------------------------------------------------------
# Test keyframe simplification
# ---------------------------------------------------------------------------

class TestKeyframeSimplification:
    """Tests for the simplify_keyframes function."""

    def test_no_simplification_needed(self) -> None:
        """Keep all keyframes when count is below max."""
        keyframes = [
            AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
            AnimKeyframe(time=0.5, value=(0.0, 2.0, 0.0)),
            AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
        ]
        result = simplify_keyframes(keyframes, max_count=10)
        assert len(result) == 3

    def test_simplification_reduces_count(self) -> None:
        """Reduce a large number of keyframes to max_count."""
        keyframes = [
            AnimKeyframe(time=i * 0.01, value=(0.0, float(i), 0.0))
            for i in range(100)
        ]
        result = simplify_keyframes(keyframes, max_count=10)
        assert len(result) == 10

    def test_simplification_keeps_first_and_last(self) -> None:
        """Always keep the first and last keyframes."""
        keyframes = [
            AnimKeyframe(time=i * 0.01, value=(0.0, float(i), 0.0))
            for i in range(50)
        ]
        result = simplify_keyframes(keyframes, max_count=5)
        assert result[0].time == keyframes[0].time
        assert result[-1].time == keyframes[-1].time

    def test_simplification_two_keyframes(self) -> None:
        """With max_count=2, only keep first and last."""
        keyframes = [
            AnimKeyframe(time=i * 0.1, value=(0.0, float(i), 0.0))
            for i in range(20)
        ]
        result = simplify_keyframes(keyframes, max_count=2)
        assert len(result) == 2
        assert result[0] is keyframes[0]
        assert result[1] is keyframes[-1]

    def test_simplification_empty_input(self) -> None:
        """Handle empty input."""
        result = simplify_keyframes([], max_count=10)
        assert result == []


# ---------------------------------------------------------------------------
# Test Luau code generation
# ---------------------------------------------------------------------------

class TestLuauGeneration:
    """Tests for Luau TweenService code generation."""

    def test_generate_position_tween(self) -> None:
        """Generate a simple position tween script."""
        clip = AnimClip(
            name="open",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
                ],
            )],
        )

        luau = generate_tween_script(clip)

        assert "TweenService" in luau
        assert "local target" in luau
        assert "playAnimation" in luau
        assert "Position" in luau
        assert "Vector3.new" in luau
        # The 4 m Unity Y offset must be scaled to studs (4 * 3.571 =
        # 14.284), NOT emitted raw. Raw +4 studs is the unscaled-offset
        # bug the legacy door_tween coherence pack existed to mask.
        assert "14.2840" in luau
        assert "Vector3.new(0.0000, 4.0000, 0.0000)" not in luau

    def test_position_tween_scales_offset_to_studs(self) -> None:
        """Regression: a Unity position curve in METRES must be tweened in
        STUDS (offset * config.STUDS_PER_METER). A 4 m door-open curve
        should produce a +14.284 stud Y offset, not a raw +4. Before the
        scale fix the inline_tween driver moved the door by an imperceptible
        +4 studs, which is why the legacy door_tween pack masked it."""
        import config

        clip = AnimClip(
            name="door_open",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(1.0, 4.0, 2.0)),
                ],
            )],
        )

        luau = generate_tween_script(clip)

        s = config.STUDS_PER_METER
        # x, y scaled positive; z negated (coord conversion) then scaled.
        expected = f"Vector3.new({1.0 * s:.4f}, {4.0 * s:.4f}, {-2.0 * s:.4f})"
        assert expected in luau, (
            f"expected scaled offset {expected!r} in:\n{luau}"
        )
        # The raw, unscaled offset must NOT appear.
        assert "Vector3.new(1.0000, 4.0000, -2.0000)" not in luau

    def test_animation_events_scheduled_before_blocking_tweens(self) -> None:
        """task.delay(event.time, ...) must be emitted before the tween
        chain inside playAnimation. The curve helpers emit
        Completed:Wait() which blocks; scheduling events after them makes
        them fire after the clip has already finished, not at the event's
        clip-local time.
        """
        from converter.animation_converter import AnimEvent

        clip = AnimClip(
            name="event_clip",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
                ],
            )],
            events=[
                AnimEvent(time=0.5, function_name="MidEvent"),
            ],
        )

        luau = generate_tween_script(clip)

        # Locate the playAnimation body and confirm task.delay precedes any
        # Completed:Wait() — the symptom: delay sits after the blocking
        # tween chain so events fire after the clip finishes.
        body = luau.split("function playAnimation(target)", 1)[1]
        body = body.split("\nend\n", 1)[0]
        delay_idx = body.find("task.delay")
        wait_idx = body.find("Completed:Wait")
        assert delay_idx != -1, "expected a task.delay for the event"
        assert wait_idx != -1, "expected a Completed:Wait in tween chain"
        assert delay_idx < wait_idx, (
            "task.delay(event.time, ...) must be emitted before the "
            "blocking Completed:Wait() so events fire at clip-local time, "
            "not after the chain finishes"
        )

    def test_generate_rotation_tween(self) -> None:
        """Generate a rotation tween script from quaternion keyframes."""
        clip = AnimClip(
            name="rotate",
            duration=2.0,
            loop=True,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="rotation",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0, 1.0)),
                    AnimKeyframe(time=2.0, value=(0.0, 0.7071, 0.0, 0.7071)),
                ],
            )],
        )

        luau = generate_tween_script(clip)

        assert "TweenService" in luau
        assert "CFrame" in luau
        assert "math.rad" in luau
        # Looping animation now uses a task.spawn loop with a Heartbeat yield
        # floor (placement-order-robust scaffold), not a top-level `while true do`.
        assert "task.spawn(" in luau
        assert "RunService.Heartbeat:Wait()" in luau
        assert "while true do" not in luau

    def test_generate_scale_tween(self) -> None:
        """Generate a scale tween script."""
        clip = AnimClip(
            name="grow",
            duration=0.5,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="scale",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(1.0, 1.0, 1.0)),
                    AnimKeyframe(time=0.5, value=(2.0, 2.0, 2.0)),
                ],
            )],
        )

        luau = generate_tween_script(clip)

        assert "Size" in luau
        assert "baseSize" in luau

    def test_generate_child_path_tween(self) -> None:
        """Generate tweens targeting a child object via path."""
        clip = AnimClip(
            name="child_move",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="Base10/Box1",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 2.0)),
                    AnimKeyframe(time=1.0, value=(0.0, 0.0, -2.0)),
                ],
            )],
        )

        luau = generate_tween_script(clip)

        assert "FindFirstChild" in luau
        assert '"Base10"' in luau
        assert '"Box1"' in luau

    def test_generate_with_bool_parameter(self) -> None:
        """Generate parameter-driven animation (like door open/close)."""
        clip = AnimClip(
            name="open",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
                ],
            )],
        )

        controller = AnimatorController(
            name="door",
            parameters=[AnimParameter(name="open", param_type=4)],
        )

        luau = generate_tween_script(clip, controller=controller)

        assert "GetAttributeChangedSignal" in luau
        assert '"open"' in luau
        assert "SetAttribute" in luau

    def test_generate_with_int_parameter(self) -> None:
        """Generate animation driven by an int parameter."""
        clip = AnimClip(
            name="holder1",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(1.0, 0.0, 0.0)),
                ],
            )],
        )

        controller = AnimatorController(
            name="PlaneHolder",
            parameters=[AnimParameter(name="actionNumber", param_type=3)],
        )

        luau = generate_tween_script(clip, controller=controller)

        assert "GetAttributeChangedSignal" in luau
        assert '"actionNumber"' in luau

    def test_generate_empty_clip(self) -> None:
        """Return empty string for a clip with no curves."""
        clip = AnimClip(name="empty", duration=1.0, loop=False, sample_rate=60)
        luau = generate_tween_script(clip)
        assert luau == ""

    def test_generate_single_keyframe_curve(self) -> None:
        """Skip curves with only one keyframe (no motion)."""
        clip = AnimClip(
            name="static",
            duration=1.0,
            loop=False,
            sample_rate=60,
            curves=[AnimCurve(
                property_type="position",
                path="",
                keyframes=[AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0))],
            )],
        )

        luau = generate_tween_script(clip)
        # Script should be generated but won't have any tween calls
        # (the curve has only 1 keyframe, so no tween is generated)
        assert "playAnimation" in luau


# ---------------------------------------------------------------------------
# Placement-order-robust binding (door-binding-race) — AC1..AC14
# ---------------------------------------------------------------------------

def _pos_curve(path: str = "") -> AnimCurve:
    """A two-keyframe position curve (survives simplification -> has content)."""
    return AnimCurve(
        property_type="position",
        path=path,
        keyframes=[
            AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
            AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
        ],
    )


def _bool_clip(name: str = "open", loop: bool = False) -> AnimClip:
    return AnimClip(name=name, duration=1.0, loop=loop, sample_rate=60,
                    curves=[_pos_curve()])


def _bool_controller(param: str = "open") -> AnimatorController:
    return AnimatorController(name="door",
                              parameters=[AnimParameter(name=param, param_type=4)])


def _int_controller(param: str = "actionNumber") -> AnimatorController:
    return AnimatorController(name="holder",
                              parameters=[AnimParameter(name=param, param_type=3)])


def _loop_clip(name: str = "Flying") -> AnimClip:
    return AnimClip(name=name, duration=2.0, loop=True, sample_rate=60,
                    curves=[_pos_curve()])


def _once_clip(name: str = "open") -> AnimClip:
    return AnimClip(name=name, duration=1.0, loop=False, sample_rate=60,
                    curves=[_pos_curve()])


class TestPlacementRobustBinding:
    """Structural assertions on the placement-order-robust emitted scaffold."""

    def test_ac1_bool_late_arrival(self) -> None:
        luau = generate_tween_script(_bool_clip(), game_object_name="Door",
                                     controller=_bool_controller())
        assert "workspace.DescendantAdded" in luau
        assert 'GetAttributeChangedSignal("open")' in luau

    def test_ac2_int_late_arrival(self) -> None:
        luau = generate_tween_script(
            AnimClip(name="holder1", duration=1.0, loop=False, sample_rate=60,
                     curves=[_pos_curve()]),
            game_object_name="Plane", controller=_int_controller())
        assert "workspace.DescendantAdded" in luau
        assert 'GetAttributeChangedSignal("actionNumber")' in luau

    def test_ac3_loop_late_arrival_yield_floor(self) -> None:
        luau = generate_tween_script(_loop_clip(), game_object_name="Plane")
        assert "workspace.DescendantAdded" in luau
        assert "task.spawn(" in luau
        assert "RunService.Heartbeat:Wait()" in luau
        assert "while true do" not in luau

    def test_ac4_play_once_late_arrival(self) -> None:
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        assert "workspace.DescendantAdded" in luau
        assert "bindTarget" in luau
        assert "playAnimation(_t)" in luau
        # No bare top-level playAnimation() call (concrete-arg only).
        assert "\nplayAnimation()" not in luau
        assert "\tplayAnimation()" not in luau

    def test_ac5_prefab_scoped_no_listener(self) -> None:
        for clip, controller in (
            (_bool_clip(), _bool_controller()),
            (_once_clip(), _int_controller()),
            (_loop_clip(), None),
            (_once_clip(), None),
        ):
            luau = generate_tween_script(clip, game_object_name="Door",
                                         controller=controller, prefab_scoped=True)
            # The listener/fanout are emitted unconditionally inside the source but
            # guarded at runtime by `if not _ownerIsContainer`. Structural check:
            # both the eager fanout (`workspace:GetDescendants()`) and the
            # late-arrival listener (`workspace.DescendantAdded`) appear AFTER the
            # guard and NOT before it — the embedded path binds only `target`.
            assert "if not _ownerIsContainer then" in luau
            guard_idx = luau.index("if not _ownerIsContainer then")
            fanout_idx = luau.index("workspace:GetDescendants()")
            listener_idx = luau.index("workspace.DescendantAdded")
            assert fanout_idx > guard_idx
            assert listener_idx > guard_idx
            # Neither appears before the guard (no earlier occurrence).
            assert "workspace:GetDescendants()" not in luau[:guard_idx]
            assert "workspace.DescendantAdded" not in luau[:guard_idx]

    def test_ac6_early_return_gated(self) -> None:
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        assert "if not target and _ownerIsContainer then" in luau
        # No bare unconditional prologue early-return.
        assert "if not target then" not in luau

    def test_ac7_no_unguarded_target_deref(self) -> None:
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        # _initialTarget DECLARATION appears once, before the `if target then` guard.
        decl_idx = luau.index("local _initialTarget = target")
        guard_idx = luau.index("if target then")
        assert decl_idx < guard_idx
        # Model-normalization (target:IsA('Model')) appears only inside the guard.
        norm_idx = luau.index("if target:IsA('Model') then")
        assert norm_idx > guard_idx
        # The value re-assignment of _initialTarget (indented, inside the guard).
        reassign_idx = luau.index("    _initialTarget = target")
        assert reassign_idx > guard_idx

    def test_ac8_idempotency(self) -> None:
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        assert '__mode = "k"' in luau
        assert "if _bound[" in luau

    def test_ac9_apply_current_state_on_bind_bool(self) -> None:
        luau = generate_tween_script(_bool_clip(), game_object_name="Door",
                                     controller=_bool_controller())
        # Default write guarded by == nil.
        assert 'if _t:GetAttribute("open") == nil then' in luau
        assert '_t:SetAttribute("open", false)' in luau
        # On-bind snap reads the attribute and conditionally plays.
        assert 'if _t:GetAttribute("open") and not _isActive then' in luau

    def test_ac9_on_bind_snap_isactive_reset_semantics(self) -> None:
        """The on-bind snap must mirror the changed-signal handler's _isActive
        semantics per clip-loop: a NON-LOOP clip plays to completion so the snap
        clears _isActive (else a target bound while already-open latches active
        and ignores later toggles); a LOOP clip leaves _isActive true (the
        handler clears it on the open->false edge)."""
        # Helper: isolate the on-bind snap block (from the snap comment to the
        # first `\tend` that closes the `if ... then`).
        def _snap_block(luau: str) -> str:
            marker = "-- apply-current-state-on-bind"
            start = luau.index(marker)
            end = luau.index("\n\tend", start) + len("\n\tend")
            return luau[start:end]

        # NON-LOOP bool clip: snap ends with the _isActive reset.
        non_loop = generate_tween_script(_bool_clip(loop=False),
                                         game_object_name="Door",
                                         controller=_bool_controller())
        non_loop_snap = _snap_block(non_loop)
        assert "\t\tplayAnimation(_t)" in non_loop_snap
        assert "\t\t_isActive = false" in non_loop_snap
        # The reset is the LAST statement before the block closes.
        assert non_loop_snap.endswith("\t\t_isActive = false\n\tend")

        # LOOP bool clip: snap does NOT reset _isActive (left true).
        loop = generate_tween_script(_bool_clip(loop=True),
                                     game_object_name="Door",
                                     controller=_bool_controller())
        loop_snap = _snap_block(loop)
        assert "\t\tplayAnimation(_t)" in loop_snap
        assert "_isActive = false" not in loop_snap

    def test_ac10_apply_current_state_on_bind_int(self) -> None:
        luau = generate_tween_script(
            AnimClip(name="holder1", duration=1.0, loop=False, sample_rate=60,
                     curves=[_pos_curve()]),
            game_object_name="Plane", controller=_int_controller())
        assert 'if _t:GetAttribute("actionNumber") == nil then' in luau
        assert '_t:SetAttribute("actionNumber", 0)' in luau
        # On-bind snap conditioned on a non-default value.
        assert '~= nil and _t:GetAttribute("actionNumber") ~= 0 then' in luau

    def test_ac11_match_name_compile_time_literal(self) -> None:
        # Explicit game_object_name -> literal "Door".
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        assert 'local _matchNames = { "Door" }' in luau
        # Empty name + single curve root "Plane" -> literal "Plane".
        clip = AnimClip(name="fly", duration=1.0, loop=False, sample_rate=60,
                        curves=[_pos_curve(path="Plane")])
        luau2 = generate_tween_script(clip)
        assert 'local _matchNames = { "Plane" }' in luau2
        # No emission keys the listener on a runtime target.Name match expression.
        assert "_inst.Name == target.Name" not in luau2

    def test_ac12_no_regression_boot_present_bool(self) -> None:
        luau = generate_tween_script(_bool_clip(), game_object_name="Door",
                                     controller=_bool_controller())
        assert "SetAttribute" in luau
        assert "GetAttributeChangedSignal" in luau
        assert "bindTarget" in luau
        # Removed shapes are gone.
        assert "_targets" not in luau
        assert "while true do" not in luau

    def test_ac13_loop_and_once_share_scaffold(self) -> None:
        loop_luau = generate_tween_script(_loop_clip(), game_object_name="Plane")
        once_luau = generate_tween_script(_once_clip(), game_object_name="Door")
        for luau in (loop_luau, once_luau):
            assert "workspace:GetDescendants()" in luau
            assert "if not _ownerIsContainer then" in luau

    def test_ac14_match_name_superset(self) -> None:
        luau = generate_tween_script(_once_clip(), game_object_name="Door")
        # _matchNames built from the compile-time literal...
        assert 'local _matchNames = { "Door" }' in luau
        # ...and the resolved target.Name appended under an `if target` guard.
        assert "if target and not _seenName[target.Name] then" in luau
        assert "table.insert(_matchNames, target.Name)" in luau

    def test_ac3_contentless_loop_skips_spin(self) -> None:
        # A loop clip whose only curve simplifies to <2 keyframes must not emit
        # the task.spawn loop (D9 — would spin); it plays once instead.
        clip = AnimClip(
            name="static", duration=1.0, loop=True, sample_rate=60,
            curves=[AnimCurve(property_type="position", path="",
                              keyframes=[AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0))])],
        )
        luau = generate_tween_script(clip, game_object_name="Door")
        assert "task.spawn(" not in luau
        assert "while true do" not in luau
        assert "playAnimation(_t)" in luau

    def test_match_names_multiple_curve_roots_empty_name(self) -> None:
        """Empty game_object_name with curves under 2+ distinct roots: the
        match precedence falls through to sorted(curve_roots), so BOTH root
        literals end up in the compile-time `_matchNames` initializer (the
        ONLY match available on the nil-boot path) and the fanout/listener
        match each."""
        def _curve(path: str) -> AnimCurve:
            return AnimCurve(
                property_type="position",
                path=path,
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                    AnimKeyframe(time=1.0, value=(0.0, 4.0, 0.0)),
                ],
            )

        clip = AnimClip(
            name="multi", duration=1.0, loop=False, sample_rate=60,
            curves=[_curve("Body/X"), _curve("Wing/Y")],
        )
        # Empty name, no controller -> play-once placement-robust scaffold.
        luau = generate_tween_script(clip, game_object_name="")

        # curve_roots is sorted, so the literal order is deterministic.
        assert 'local _matchNames = { "Body", "Wing" }' in luau
        # Both roots also drive the boot FindFirstChild fallback chain.
        assert 'workspace:FindFirstChild("Body", true)' in luau
        assert 'workspace:FindFirstChild("Wing", true)' in luau

    def test_contentless_loop_via_param_driven_path_no_spin(self) -> None:
        """D9 through _generate_parameter_driven_playback's auto-play loop
        fallback: a LOOP clip whose every curve simplifies to <2 keyframes,
        with a controller whose only parameter is neither bool (4) nor int
        (3), routes to the `loop` action body. Because the clip has no
        surviving tween content, the loop emits a single `playAnimation(_t)`
        — NOT the `task.spawn`/`while ... do` spin (which, given a no-op body,
        would busy-loop with only the Heartbeat yield as a floor)."""
        clip = AnimClip(
            name="static", duration=1.0, loop=True, sample_rate=60,
            curves=[AnimCurve(
                property_type="position", path="",
                keyframes=[AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0))],
            )],
        )
        # A float (type 1) param has no bool/int handler, so
        # _generate_parameter_driven_playback falls through to the clip.loop
        # auto-play branch -> _emit_placement_robust_binding(..., "loop", "").
        controller = AnimatorController(
            name="mover",
            parameters=[AnimParameter(name="Speed", param_type=1)],
        )
        luau = generate_tween_script(
            clip, game_object_name="Door", controller=controller,
        )

        # We did route through the auto-play loop fallback...
        assert "-- Auto-play looping animation" in luau
        # ...but the contentless D9 guard collapses it to a single play, no spin.
        assert "playAnimation(_t)" in luau
        assert "task.spawn(" not in luau
        assert "while true do" not in luau
        assert "RunService.Heartbeat:Wait()" not in luau


# ---------------------------------------------------------------------------
# Test quaternion-to-Euler conversion
# ---------------------------------------------------------------------------

class TestQuaternionToEuler:
    """Tests for quaternion to Euler angle conversion."""

    def test_identity_quaternion(self) -> None:
        """Identity quaternion -> zero Euler angles."""
        ex, ey, ez = _quat_to_euler_degrees(0, 0, 0, 1)
        assert abs(ex) < 0.01
        assert abs(ey) < 0.01
        assert abs(ez) < 0.01

    def test_90_degree_y_rotation(self) -> None:
        """90-degree rotation around Y axis."""
        import math
        # Quaternion for 90 deg around Y: (0, sin(45), 0, cos(45))
        s = math.sin(math.radians(45))
        c = math.cos(math.radians(45))
        ex, ey, ez = _quat_to_euler_degrees(0, s, 0, c)
        assert abs(ey - 90.0) < 0.5


# ---------------------------------------------------------------------------
# Test project-level discovery
# ---------------------------------------------------------------------------

class TestAnimationDiscovery:
    """Tests for project-level animation discovery."""

    def test_discover_in_empty_project(self, tmp_path: Path) -> None:
        """Return empty lists for a project with no Assets folder."""
        clips, controllers = discover_animations(tmp_path)
        assert clips == []
        assert controllers == []

    def test_discover_with_anim_files(self, tmp_path: Path) -> None:
        """Discover .anim files in a project."""
        assets = tmp_path / "Assets" / "Animations"
        assets.mkdir(parents=True)

        (assets / "open.anim").write_text(MINIMAL_ANIM_YAML, encoding="utf-8")
        (assets / "rotate.anim").write_text(ROTATION_ANIM_YAML, encoding="utf-8")

        clips, controllers = discover_animations(tmp_path)

        assert len(clips) == 2
        assert len(controllers) == 0
        clip_names = {c.name for c in clips}
        assert "test_open" in clip_names
        assert "test_rotate" in clip_names

    def test_discover_with_controller_files(self, tmp_path: Path) -> None:
        """Discover .controller files in a project."""
        assets = tmp_path / "Assets" / "Animations"
        assets.mkdir(parents=True)

        (assets / "door.controller").write_text(MINIMAL_CONTROLLER_YAML, encoding="utf-8")

        clips, controllers = discover_animations(tmp_path)

        assert len(clips) == 0
        assert len(controllers) == 1
        assert controllers[0].name == "TestController"

    def test_convert_animations_generates_scripts(self, tmp_path: Path) -> None:
        """convert_animations generates Luau scripts for standalone clips."""
        assets = tmp_path / "Assets" / "Animations"
        assets.mkdir(parents=True)

        (assets / "open.anim").write_text(MINIMAL_ANIM_YAML, encoding="utf-8")

        result = convert_animations(tmp_path)

        assert result.total_clips == 1
        assert result.total_controllers == 0
        # Standalone clip (not referenced by controller) should generate a script
        assert result.total_scripts_generated == 1
        assert len(result.generated_scripts) == 1

        script_name, luau_source = result.generated_scripts[0]
        assert "Anim_test_open" in script_name
        assert "TweenService" in luau_source


# ---------------------------------------------------------------------------
# Test integration with real SimpleFPS .anim files (if available)
# ---------------------------------------------------------------------------

from tests._project_paths import SIMPLEFPS_PATH as _SIMPLE_FPS_PATH, is_populated as _is_populated


@pytest.mark.skipif(
    not _is_populated(_SIMPLE_FPS_PATH),
    reason="SimpleFPS test project not available",
)
class TestRealAnimFiles:
    """Integration tests using actual SimpleFPS animation files."""

    def test_parse_door_open_anim(self) -> None:
        """Parse the actual door open.anim file from SimpleFPS."""
        anim_path = _SIMPLE_FPS_PATH / "Assets/AssetPack/SciFi_Door/Animation/open.anim"
        clip = parse_anim_file(anim_path)

        assert clip is not None
        assert clip.name == "open"
        assert clip.duration == pytest.approx(1.0)
        assert clip.loop is False
        assert len(clip.curves) >= 1  # At least position curve

        # The door opens by moving Y +4
        pos_curve = next(c for c in clip.curves if c.property_type == "position")
        assert len(pos_curve.keyframes) == 2
        assert pos_curve.keyframes[1].value[1] == pytest.approx(4.0)

    def test_parse_door_close_anim(self) -> None:
        """Parse the actual door close.anim file."""
        anim_path = _SIMPLE_FPS_PATH / "Assets/AssetPack/SciFi_Door/Animation/close.anim"
        clip = parse_anim_file(anim_path)

        assert clip is not None
        assert clip.name == "close"
        assert clip.loop is True  # close.anim has m_LoopTime: 1

        pos_curve = next(c for c in clip.curves if c.property_type == "position")
        # Starts at Y=4, ends at Y=0
        assert pos_curve.keyframes[0].value[1] == pytest.approx(4.0)
        assert pos_curve.keyframes[1].value[1] == pytest.approx(0.0)

    def test_parse_door_controller(self) -> None:
        """Parse the actual door.controller file."""
        ctrl_path = _SIMPLE_FPS_PATH / "Assets/AssetPack/SciFi_Door/Animation/door.controller"
        ctrl = parse_controller_file(ctrl_path)

        assert ctrl is not None
        assert ctrl.name == "door"
        assert len(ctrl.parameters) == 1
        assert ctrl.parameters[0].name == "open"
        assert ctrl.parameters[0].param_type == 4  # Bool

        # Should have Idle, open, close states
        state_names = {s.name for s in ctrl.states}
        assert "Idle" in state_names
        assert "open" in state_names
        assert "close" in state_names

    def test_parse_hostile_plane_controller(self) -> None:
        """Parse the HostilePlane.controller file (single auto-play state)."""
        ctrl_path = _SIMPLE_FPS_PATH / "Assets/Animations/HostilePlane/HostilePlane.controller"
        ctrl = parse_controller_file(ctrl_path)

        assert ctrl is not None
        assert ctrl.name == "HostilePlane"
        assert len(ctrl.parameters) == 0
        assert len(ctrl.states) == 1
        assert ctrl.states[0].name == "Flying"

    def test_parse_plane_holder_controller(self) -> None:
        """Parse PlaneHolder.controller (int parameter, multiple states)."""
        ctrl_path = _SIMPLE_FPS_PATH / "Assets/Animations/PlaneHolder/PlaneHolder.controller"
        ctrl = parse_controller_file(ctrl_path)

        assert ctrl is not None
        assert ctrl.name == "PlaneHolder"
        assert len(ctrl.parameters) == 1
        assert ctrl.parameters[0].name == "actionNumber"
        assert ctrl.parameters[0].param_type == 3  # Int

        state_names = {s.name for s in ctrl.states}
        assert "holder1" in state_names
        assert "holder2" in state_names
        assert "holder3" in state_names
        assert "Idle" in state_names

    def test_parse_holder1_anim(self) -> None:
        """Parse holder1.anim which targets child objects by path."""
        anim_path = _SIMPLE_FPS_PATH / "Assets/Animations/PlaneHolder/holder1.anim"
        clip = parse_anim_file(anim_path)

        assert clip is not None
        assert clip.name == "holder1"
        # This anim has position curves with paths like "Base10/Box1"
        assert len(clip.curves) >= 1
        # At least some curves should target child objects
        child_curves = [c for c in clip.curves if c.path]
        assert len(child_curves) >= 1

    def test_generate_door_animation_script(self) -> None:
        """Generate a complete door animation script."""
        anim_path = _SIMPLE_FPS_PATH / "Assets/AssetPack/SciFi_Door/Animation/open.anim"
        ctrl_path = _SIMPLE_FPS_PATH / "Assets/AssetPack/SciFi_Door/Animation/door.controller"

        clip = parse_anim_file(anim_path)
        ctrl = parse_controller_file(ctrl_path)

        assert clip is not None
        assert ctrl is not None

        luau = generate_tween_script(clip, controller=ctrl)

        assert "TweenService" in luau
        assert "playAnimation" in luau
        assert "open" in luau  # parameter name
        assert "GetAttributeChangedSignal" in luau

    def test_full_project_discovery(self) -> None:
        """Discover all animations in the SimpleFPS project."""
        clips, controllers = discover_animations(_SIMPLE_FPS_PATH)

        # SimpleFPS has: open.anim, close.anim, fly.anim, Flying.anim,
        #                holder1.anim, holder2.anim, holder3.anim
        assert len(clips) >= 7

        # Controllers: door.controller, HostilePlane.controller,
        #              PlaneHolder.controller, Plane Flying.controller
        assert len(controllers) >= 4

    def test_full_project_conversion(self) -> None:
        """Run full animation conversion on SimpleFPS project."""
        from unity.guid_resolver import build_guid_index

        guid_index = build_guid_index(_SIMPLE_FPS_PATH)
        result = convert_animations(_SIMPLE_FPS_PATH, guid_index=guid_index)

        assert result.total_clips >= 7
        assert result.total_controllers >= 4
        # Should generate at least some scripts
        assert result.total_scripts_generated >= 1


# ---------------------------------------------------------------------------
# Test pipeline integration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Tests for animation converter integration with the pipeline."""

    def test_pipeline_has_convert_animations_phase(self) -> None:
        """The pipeline includes the convert_animations phase."""
        from converter.pipeline import PHASES
        assert "convert_animations" in PHASES
        # Should be after transpile_scripts and before convert_scene
        idx = PHASES.index("convert_animations")
        assert PHASES[idx - 1] == "transpile_scripts"
        assert PHASES[idx + 1] == "convert_scene"

    def test_conversion_context_has_animation_fields(self) -> None:
        """ConversionContext tracks animation stats."""
        from core.conversion_context import ConversionContext
        ctx = ConversionContext()
        assert hasattr(ctx, "total_animations")
        assert hasattr(ctx, "converted_animations")
        assert ctx.total_animations == 0
        assert ctx.converted_animations == 0

    def test_pipeline_state_has_animation_result(self) -> None:
        """PipelineState has an animation_result field."""
        from converter.pipeline import PipelineState
        state = PipelineState()
        assert hasattr(state, "animation_result")
        assert state.animation_result is None


class TestStateMachineScriptRetired:
    """Humanoid AnimatorControllers emit no animation output.

    Skeletal/character animation is unsupported (see docs/UNSUPPORTED.md):
    a humanoid controller emits neither a ``_StateMachine`` Script nor any
    other animation script — its clips are surfaced to UNCONVERTED.md.
    """

    def test_humanoid_controller_with_transitions_emits_no_statemachine_script(
        self, tmp_path: Path,
    ) -> None:
        assets = tmp_path / "Assets" / "Animations"
        assets.mkdir(parents=True)

        # Humanoid clip — a Hips curve makes it non-transform-only.
        (assets / "Walk.anim").write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &7400000
            AnimationClip:
              m_Name: Walk
              m_PositionCurves:
              - curve:
                  serializedVersion: 2
                  m_Curve:
                  - serializedVersion: 3
                    time: 0
                    value: {x: 0, y: 0, z: 0}
                    inSlope: {x: 0, y: 0, z: 0}
                    outSlope: {x: 0, y: 0, z: 0}
                  - serializedVersion: 3
                    time: 1
                    value: {x: 1, y: 2, z: 3}
                    inSlope: {x: 0, y: 0, z: 0}
                    outSlope: {x: 0, y: 0, z: 0}
                path: Hips
              m_RotationCurves: []
              m_EulerCurves: []
              m_ScaleCurves: []
              m_AnimationClipSettings:
                serializedVersion: 2
                m_LoopTime: 1
                m_StopTime: 1
        """))
        (assets / "Walk.anim.meta").write_text(textwrap.dedent("""\
            fileFormatVersion: 2
            guid: abcd1234abcd1234abcd1234abcd1234
        """))

        # Controller with 2 states + a transition — this is exactly the
        # `humanoid_clips and has_transitions and len(states) >= 2` branch
        # that used to trigger generate_state_machine_script.
        (assets / "RetiredSMCtrl.controller").write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: RetiredSMCtrl
              m_AnimatorParameters:
              - m_Name: Speed
                m_Type: 1
                m_DefaultFloat: 0
                m_DefaultInt: 0
                m_DefaultBool: 0
              m_AnimatorLayers:
              - serializedVersion: 5
                m_Name: Base Layer
                m_StateMachine:
                  fileID: 300
            --- !u!1107 &300
            AnimatorStateMachine:
              m_ChildStates:
              - serializedVersion: 1
                m_State:
                  fileID: 400
              - serializedVersion: 1
                m_State:
                  fileID: 401
              m_DefaultState:
                fileID: 400
            --- !u!1101 &500
            AnimatorStateTransition:
              m_Name: IdleToWalk
              m_Conditions:
              - m_ConditionMode: 3
                m_ConditionEvent: Speed
                m_EventTreshold: 0.1
              m_DstState: {fileID: 401}
              m_TransitionDuration: 0.1
              m_HasExitTime: 0
              m_ExitTime: 0.9
            --- !u!1102 &400
            AnimatorState:
              m_Name: Idle
              m_Speed: 1
              m_Transitions:
              - {fileID: 500}
              m_Motion:
                fileID: 7400000
                guid: abcd1234abcd1234abcd1234abcd1234
                type: 2
            --- !u!1102 &401
            AnimatorState:
              m_Name: Walk
              m_Speed: 1
              m_Transitions: []
              m_Motion:
                fileID: 7400000
                guid: abcd1234abcd1234abcd1234abcd1234
                type: 2
        """))

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(tmp_path, guid_index=guid_index)

        # No per-controller state-machine Script is emitted.
        sm_scripts = [n for n, _ in result.generated_scripts if n.endswith("_StateMachine")]
        assert sm_scripts == [], f"_StateMachine script not retired: {sm_scripts}"

        # The humanoid clip is unsupported — no animation script of any kind.
        assert result.generated_scripts == [], result.generated_scripts

        # The humanoid clip is surfaced to UNCONVERTED.md instead.
        items = [e["item"] for e in result.unconverted if e["category"] == "animation_clip"]
        assert any("Walk" in i for i in items), result.unconverted
        for entry in result.unconverted:
            if entry["category"] == "animation_clip" and "Walk" in entry["item"]:
                assert "skeletal" in entry["reason"].lower()


# ---------------------------------------------------------------------------
# Humanoid / skeletal clips are unsupported (see docs/UNSUPPORTED.md)
# ---------------------------------------------------------------------------

class TestHumanoidClipsUnsupported:
    """Skeletal/character animation is unsupported: humanoid clips are
    surfaced to UNCONVERTED.md and produce no animation script."""

    def test_humanoid_controller_surfaces_clip_to_unconverted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "Assets" / "Animations"
            assets.mkdir(parents=True)

            anim_yaml = textwrap.dedent("""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!74 &7400000
                AnimationClip:
                  m_ObjectHideFlags: 0
                  m_Name: Walk
                  m_PositionCurves:
                  - curve:
                      serializedVersion: 2
                      m_Curve:
                      - serializedVersion: 3
                        time: 0
                        value: {x: 0, y: 0, z: 0}
                        inSlope: {x: 0, y: 0, z: 0}
                        outSlope: {x: 0, y: 0, z: 0}
                      - serializedVersion: 3
                        time: 1
                        value: {x: 1, y: 2, z: 3}
                        inSlope: {x: 0, y: 0, z: 0}
                        outSlope: {x: 0, y: 0, z: 0}
                    path: Hips
                  m_RotationCurves: []
                  m_EulerCurves: []
                  m_ScaleCurves: []
                  m_AnimationClipSettings:
                    serializedVersion: 2
                    m_LoopTime: 1
                    m_StopTime: 1
            """)
            (assets / "Walk.anim").write_text(anim_yaml)
            (assets / "Walk.anim.meta").write_text(textwrap.dedent("""\
                fileFormatVersion: 2
                guid: abcd1234abcd1234abcd1234abcd1234
            """))

            ctrl_yaml = textwrap.dedent("""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!91 &9100000
                AnimatorController:
                  m_ObjectHideFlags: 0
                  m_Name: HumanoidCtrl
                  m_AnimatorParameters: []
                  m_AnimatorLayers:
                  - serializedVersion: 5
                    m_Name: Base Layer
                    m_StateMachine:
                      fileID: 300
                --- !u!1107 &300
                AnimatorStateMachine:
                  m_ChildStates:
                  - serializedVersion: 1
                    m_State:
                      fileID: 400
                  m_DefaultState:
                    fileID: 400
                --- !u!1102 &400
                AnimatorState:
                  m_Name: Walk
                  m_Motion:
                    fileID: 7400000
                    guid: abcd1234abcd1234abcd1234abcd1234
                    type: 2
                  m_Speed: 1
                  m_Transitions: []
            """)
            (assets / "HumanoidCtrl.controller").write_text(ctrl_yaml)

            from unity.guid_resolver import build_guid_index
            guid_index = build_guid_index(Path(tmpdir))
            result = convert_animations(Path(tmpdir), guid_index=guid_index)

            # No animation script of any kind is emitted for the humanoid clip.
            assert result.generated_scripts == [], result.generated_scripts

            # The clip is surfaced to UNCONVERTED.md.
            clip_entries = [
                e for e in result.unconverted
                if e["category"] == "animation_clip" and "Walk" in e["item"]
            ]
            assert clip_entries, result.unconverted
            assert "skeletal" in clip_entries[0]["reason"].lower()

            # Routing records the clip as skipped, not character_animator.
            routing = result.routing.get("HumanoidCtrl", {})
            assert routing
            for decision in routing.values():
                assert decision["target"] == "skipped"

    def test_orphan_humanoid_clip_surfaced_and_not_tweened(self):
        """A standalone humanoid .anim with no controller is surfaced to
        UNCONVERTED.md and produces no Anim_* script."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "Assets" / "Animations"
            assets.mkdir(parents=True)

            anim_yaml = textwrap.dedent("""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!74 &7400000
                AnimationClip:
                  m_ObjectHideFlags: 0
                  m_Name: OrphanWalk
                  m_PositionCurves:
                  - curve:
                      serializedVersion: 2
                      m_Curve:
                      - serializedVersion: 3
                        time: 0
                        value: {x: 0, y: 0, z: 0}
                        inSlope: {x: 0, y: 0, z: 0}
                        outSlope: {x: 0, y: 0, z: 0}
                      - serializedVersion: 3
                        time: 1
                        value: {x: 1, y: 2, z: 3}
                        inSlope: {x: 0, y: 0, z: 0}
                        outSlope: {x: 0, y: 0, z: 0}
                    path: LeftUpperArm
                  m_RotationCurves: []
                  m_EulerCurves: []
                  m_ScaleCurves: []
                  m_AnimationClipSettings:
                    serializedVersion: 2
                    m_LoopTime: 1
                    m_StopTime: 1
            """)
            (assets / "OrphanWalk.anim").write_text(anim_yaml)

            result = convert_animations(Path(tmpdir))

            # No Anim_* script for the orphan humanoid clip.
            anim_scripts = [n for n, _ in result.generated_scripts if n.startswith("Anim_")]
            assert anim_scripts == [], anim_scripts

            # Surfaced to UNCONVERTED.md.
            clip_entries = [
                e for e in result.unconverted
                if e["category"] == "animation_clip" and "OrphanWalk" in e["item"]
            ]
            assert clip_entries, result.unconverted
            assert "skeletal" in clip_entries[0]["reason"].lower()


class TestPhase45Routing:
    """Phase 4.5: humanoid vs transform-only routing, blend trees, persistence."""

    def test_is_transform_only_empty_clip(self) -> None:
        """A clip with no curves is neither humanoid nor transform-only."""
        clip = AnimClip(name="empty", duration=1.0, loop=False, sample_rate=60.0)
        assert clip.is_transform_only is False

    def test_is_transform_only_non_humanoid_path(self) -> None:
        """Curves on arbitrary children route as transform-only."""
        clip = AnimClip(
            name="door",
            duration=1.0,
            loop=False,
            sample_rate=60.0,
            curves=[
                AnimCurve(property_type="position", path="Hinge",
                          keyframes=[AnimKeyframe(time=0.0, value=(0, 0, 0))]),
            ],
        )
        assert clip.is_transform_only is True

    def test_is_transform_only_humanoid_path(self) -> None:
        """Any humanoid bone reference flips to False."""
        clip = AnimClip(
            name="walk",
            duration=1.0,
            loop=True,
            sample_rate=60.0,
            curves=[
                AnimCurve(property_type="position", path="Armature/Hips/Spine",
                          keyframes=[AnimKeyframe(time=0.0, value=(0, 0, 0))]),
            ],
        )
        assert clip.is_transform_only is False

    def test_is_transform_only_mixamo_prefix(self) -> None:
        """Mixamo-style ``Armature|LeftFoot`` must strip to ``LeftFoot``."""
        clip = AnimClip(
            name="run",
            duration=1.0,
            loop=True,
            sample_rate=60.0,
            curves=[
                AnimCurve(property_type="position", path="Root|LeftFoot",
                          keyframes=[AnimKeyframe(time=0.0, value=(0, 0, 0))]),
            ],
        )
        assert clip.is_transform_only is False

    def test_bone_paths_deduped_on_parse(self, tmp_path: Path) -> None:
        """Parsed clips record each unique curve path once in bone_paths."""
        anim = tmp_path / "Rotate.anim"
        anim.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &1
            AnimationClip:
              m_Name: Rotate
              m_SampleRate: 30
              m_AnimationClipSettings:
                m_StartTime: 0
                m_StopTime: 2.0
                m_LoopTime: 1
              m_EulerCurves:
              - curve:
                  m_Curve:
                  - time: 0
                    value: {x: 0, y: 0, z: 0}
                path: Spinner
              m_ScaleCurves:
              - curve:
                  m_Curve:
                  - time: 0
                    value: {x: 1, y: 1, z: 1}
                path: Spinner
              m_PositionCurves: []
              m_RotationCurves: []
            """))
        clip = parse_anim_file(anim)
        assert clip is not None
        assert clip.bone_paths == ["Spinner"]
        assert clip.is_transform_only is True

    def test_blend_tree_parses_1d_from_controller(self, tmp_path: Path) -> None:
        """A 1D blend tree is attached to AnimatorController.blend_trees."""
        ctrl = tmp_path / "Locomotion.controller"
        ctrl.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Locomotion
              m_AnimatorParameters:
              - m_Name: Speed
                m_Type: 1
              m_AnimatorLayers:
              - serializedVersion: 5
                m_Name: Base Layer
                m_StateMachine:
                  fileID: 200
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Move
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: MoveBlend
              m_BlendType: 0
              m_BlendParameter: Speed
              m_Childs:
              - m_Threshold: 0
                m_Motion:
                  guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
              - m_Threshold: 1
                m_Motion:
                  guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
            """))
        c = parse_controller_file(ctrl)
        assert c is not None
        assert "MoveBlend" in c.blend_trees
        bt = c.blend_trees["MoveBlend"]
        assert bt.param == "Speed"
        assert [e.clip_guid for e in bt.entries] == [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ]
        move_state = next(s for s in c.states if s.name == "Move")
        assert move_state.blend_tree_name == "MoveBlend"

    def test_blend_tree_2d_is_skipped_but_fallback_clip_kept(self, tmp_path: Path) -> None:
        """2D blend trees are not emitted; the state still gets a fallback clip_guid."""
        ctrl = tmp_path / "TwoD.controller"
        ctrl.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: TwoD
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: MixMove
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: Cartesian
              m_BlendType: 1
              m_BlendParameter: X
              m_BlendParameterY: Y
              m_Childs:
              - m_Threshold: 0
                m_Motion:
                  guid: cccccccccccccccccccccccccccccccc
            """))
        c = parse_controller_file(ctrl)
        assert c is not None
        assert c.blend_trees == {}  # 2D trees are not emitted
        state = c.states[0]
        assert state.blend_tree_name == ""
        # Fallback clip_guid kept so the runtime has something to play.
        assert state.clip_guid == "cccccccccccccccccccccccccccccccc"

    def test_routing_records_per_clip_decisions(self, tmp_path: Path) -> None:
        """convert_animations writes humanoid vs transform-only routing per clip."""
        # Build a minimal project: one transform-only clip + controller.
        assets = tmp_path / "Assets"
        assets.mkdir()
        anim = assets / "Spin.anim"
        anim.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &1
            AnimationClip:
              m_Name: Spin
              m_SampleRate: 30
              m_AnimationClipSettings:
                m_StartTime: 0
                m_StopTime: 1.0
                m_LoopTime: 1
              m_EulerCurves:
              - curve:
                  m_Curve:
                  - time: 0
                    value: {x: 0, y: 0, z: 0}
                  - time: 1
                    value: {x: 0, y: 360, z: 0}
                path: Spinner
              m_PositionCurves: []
              m_RotationCurves: []
              m_ScaleCurves: []
            """))
        anim_meta = anim.with_suffix(".anim.meta")
        anim_meta.write_text("fileFormatVersion: 2\nguid: " + "d" * 32 + "\n")
        ctrl = assets / "SpinCtrl.controller"
        ctrl.write_text(textwrap.dedent(f"""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: SpinCtrl
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {{fileID: 200}}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {{fileID: 300}}
              m_DefaultState: {{fileID: 300}}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Spinning
              m_Motion:
                fileID: 7400000
                guid: {"d" * 32}
                type: 2
            """))

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(tmp_path, guid_index=guid_index)

        assert "SpinCtrl" in result.routing
        assert result.routing["SpinCtrl"]["Spin"]["target"] == "inline_tween"
        # Transform-only clip produced an inline TweenService script.
        assert any(name.startswith("Anim_SpinCtrl_Spin") for name, _ in result.generated_scripts)

    def test_parsed_scenes_filter_unreferenced_controllers(self, tmp_path: Path) -> None:
        """When parsed_scenes is set, controllers no scene references are skipped."""
        from core.unity_types import ParsedScene
        assets = tmp_path / "Assets"
        assets.mkdir()
        # Controller with a meta GUID, never referenced by any scene.
        ctrl = assets / "Ghost.controller"
        ctrl.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Ghost
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates: []
            """))
        ctrl.with_suffix(".controller.meta").write_text(
            "fileFormatVersion: 2\nguid: " + "e" * 32 + "\n"
        )
        # Scene that references a *different* controller guid.
        scene_obj = ParsedScene(
            scene_path=assets / "Level1.unity",
            referenced_animator_controller_guids={"f" * 32},
        )

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(
            tmp_path, guid_index=guid_index, parsed_scenes=[scene_obj],
        )
        assert "Ghost" in result.routing
        assert result.routing["Ghost"]["__controller__"]["target"] == "skipped"

    def test_same_name_controllers_dont_collide(self, tmp_path: Path) -> None:
        """Two AnimatorControllers sharing m_Name across distinct files emit
        independent scripts and routing entries instead of one stomping the other."""
        assets = tmp_path / "Assets"
        (assets / "PrefabA").mkdir(parents=True)
        (assets / "PrefabB").mkdir(parents=True)

        # Each prefab has its own "AnimController" — common Unity pattern.
        # GUIDs are lowercased by _parse_meta_file, so the clip and
        # controller .meta files in each prefab need genuinely distinct
        # 32-char guids (not just case-different) to avoid colliding in
        # the index.
        per_prefab = (
            ("PrefabA", "a" * 32, "1" * 32),
            ("PrefabB", "b" * 32, "2" * 32),
        )
        for sub, clip_guid, ctrl_guid in per_prefab:
            anim = assets / sub / "Spin.anim"
            anim.write_text(textwrap.dedent(f"""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!74 &7400000
                AnimationClip:
                  m_Name: Spin{sub}
                  m_SampleRate: 30
                  m_AnimationClipSettings:
                    m_StartTime: 0
                    m_StopTime: 1.0
                    m_LoopTime: 1
                  m_EulerCurves:
                  - curve:
                      m_Curve:
                      - time: 0
                        value: {{x: 0, y: 0, z: 0}}
                      - time: 1
                        value: {{x: 0, y: 360, z: 0}}
                    path: Spinner
                  m_PositionCurves: []
                  m_RotationCurves: []
                  m_ScaleCurves: []
                """))
            anim.with_suffix(".anim.meta").write_text(
                f"fileFormatVersion: 2\nguid: {clip_guid}\n"
            )

            ctrl = assets / sub / "AnimController.controller"
            ctrl.write_text(textwrap.dedent(f"""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!91 &9100000
                AnimatorController:
                  m_Name: AnimController
                  m_AnimatorLayers:
                  - m_Name: Base Layer
                    m_StateMachine: {{fileID: 200}}
                --- !u!1107 &200
                AnimatorStateMachine:
                  m_ChildStates:
                  - m_State: {{fileID: 300}}
                  m_DefaultState: {{fileID: 300}}
                --- !u!1102 &300
                AnimatorState:
                  m_Name: Spinning
                  m_Motion:
                    fileID: 7400000
                    guid: {clip_guid}
                    type: 2
                """))
            ctrl.with_suffix(".controller.meta").write_text(
                f"fileFormatVersion: 2\nguid: {ctrl_guid}\n"
            )

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(tmp_path, guid_index=guid_index)

        # Both controllers must appear in routing — exactly one is keyed by
        # the bare name, the other gets a __<hash> disambiguator.
        ctrl_routing_keys = [
            k for k in result.routing
            if k == "AnimController" or k.startswith("AnimController__")
        ]
        assert len(ctrl_routing_keys) == 2, (
            f"expected two distinct routing keys for same-name controllers, "
            f"got {sorted(result.routing)}"
        )

        # Both controllers must emit their own inline tween script. Without
        # the fix one script_name overwrites the other on disk, since
        # write_output writes f'{script_name}.luau' into scripts/animations/.
        spin_scripts = [
            name for name, _ in result.generated_scripts
            if name.startswith("Anim_AnimController")
            or name.startswith("Anim_AnimController__")
        ]
        assert len(spin_scripts) == 2, (
            f"expected two distinct generated scripts for same-name "
            f"controllers, got {spin_scripts}"
        )
        assert len(set(spin_scripts)) == 2, (
            f"generated script names must be unique, got {spin_scripts}"
        )

    def test_same_name_humanoid_clips_both_surfaced_to_unconverted(self, tmp_path: Path) -> None:
        """Two distinct humanoid AnimationClips with identical m_Name in
        one controller are each surfaced to UNCONVERTED.md — both the
        skeletal-unsupported entry and the duplicate-name collision."""
        assets = tmp_path / "Assets"
        assets.mkdir()

        # Two clips named "Walk" living at different paths — both touch
        # a humanoid bone (Hips), so both are unsupported.
        for fname, guid in (("WalkA.anim", "a" * 32), ("WalkB.anim", "b" * 32)):
            (assets / fname).write_text(textwrap.dedent("""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!74 &7400000
                AnimationClip:
                  m_Name: Walk
                  m_SampleRate: 30
                  m_AnimationClipSettings:
                    m_StartTime: 0
                    m_StopTime: 1.0
                    m_LoopTime: 1
                  m_PositionCurves:
                  - curve:
                      m_Curve:
                      - time: 0
                        value: {x: 0, y: 0, z: 0}
                      - time: 1
                        value: {x: 1, y: 0, z: 0}
                    path: Hips
                  m_RotationCurves: []
                  m_EulerCurves: []
                  m_ScaleCurves: []
                """))
            (assets / fname).with_suffix(".anim.meta").write_text(
                f"fileFormatVersion: 2\nguid: {guid}\n"
            )

        ctrl = assets / "Locomotion.controller"
        ctrl.write_text(textwrap.dedent(f"""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Locomotion
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {{fileID: 200}}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {{fileID: 301}}
              - m_State: {{fileID: 302}}
              m_DefaultState: {{fileID: 301}}
            --- !u!1102 &301
            AnimatorState:
              m_Name: Walk_A
              m_Motion:
                fileID: 7400000
                guid: {"a" * 32}
                type: 2
            --- !u!1102 &302
            AnimatorState:
              m_Name: Walk_B
              m_Motion:
                fileID: 7400000
                guid: {"b" * 32}
                type: 2
            """))

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(tmp_path, guid_index=guid_index)

        # No animation script of any kind for unsupported humanoid clips.
        assert result.generated_scripts == [], result.generated_scripts

        # Both clips are surfaced as skeletal/unsupported.
        skeletal = [
            entry for entry in result.unconverted
            if entry.get("category") == "animation_clip"
            and "skeletal" in entry.get("reason", "").lower()
        ]
        assert len(skeletal) == 2, result.unconverted

        # The duplicate-name collision is also still surfaced.
        clip_collisions = [
            entry for entry in result.unconverted
            if entry.get("category") == "animation_clip"
            and "duplicate clip name" in entry.get("reason", "")
        ]
        assert clip_collisions, (
            f"expected an UNCONVERTED entry for the duplicate clip name, "
            f"got {result.unconverted}"
        )

    def test_binary_controller_emits_unconverted_entry(self, tmp_path: Path) -> None:
        """Phase 4.5b: binary .controller files surface in UNCONVERTED.md."""
        ctrl = tmp_path / "Binary.controller"
        # Non-YAML binary signature fails is_text_yaml(); parser records
        # the entry into the supplied list and returns None.
        ctrl.write_bytes(b"\x00\x01\x02binary-not-yaml")
        out: list = []
        result = parse_controller_file(ctrl, unconverted_out=out)
        assert result is None
        assert len(out) == 1
        assert out[0]["category"] == "animator_controller"
        assert "Binary.controller" in out[0]["item"]
        assert "binary" in out[0]["reason"].lower()

    def test_2d_blend_tree_emits_unconverted_entry(self, tmp_path: Path) -> None:
        """Phase 4.5b: 2D blend trees surface in UNCONVERTED.md."""
        ctrl = tmp_path / "TwoD.controller"
        ctrl.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: TwoD
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Blend
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: Cartesian
              m_BlendType: 1
              m_BlendParameter: X
              m_Childs:
              - m_Threshold: 0
                m_Motion:
                  guid: cccccccccccccccccccccccccccccccc
            """))
        out: list = []
        c = parse_controller_file(ctrl, unconverted_out=out)
        assert c is not None
        assert len(out) == 1
        assert out[0]["category"] == "blend_tree"
        assert "TwoD/Blend" in out[0]["item"]
        assert "2D" in out[0]["reason"]

    def test_nested_2d_blend_tree_emits_unconverted_entry(self, tmp_path: Path) -> None:
        """Phase 4.5b follow-up (Codex P2 #2): a 1D blend tree whose
        nested child is itself 2D must also surface the drop in
        UNCONVERTED.md — the outer parser only reports its own
        top-level 2D case.
        """
        ctrl = tmp_path / "Nested.controller"
        ctrl.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Nested
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Move
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: Outer1D
              m_BlendType: 0
              m_BlendParameter: Speed
              m_Childs:
              - m_Threshold: 0
                m_Motion: {fileID: 401}
            --- !u!206 &401
            BlendTree:
              m_Name: Inner2D
              m_BlendType: 1
              m_BlendParameter: X
              m_BlendParameterY: Y
              m_Childs:
              - m_Threshold: 0
                m_Motion:
                  guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
            """))
        out: list = []
        c = parse_controller_file(ctrl, unconverted_out=out)
        assert c is not None
        # The outer 1D tree parses; but the nested 2D grandchild
        # surfaces an entry regardless.
        assert any(
            e["category"] == "blend_tree" and "nested" in e["item"]
            for e in out
        ), f"expected nested blend_tree entry, got: {out}"
        # And the outer tree is still emitted 1D with a flattened clip.
        assert "Outer1D" in c.blend_trees
        assert c.blend_trees["Outer1D"].entries[0].clip_guid == "a" * 32

    def test_binary_controller_entry_carries_meta_guid(self, tmp_path: Path) -> None:
        """Phase 4.5b follow-up (Codex P2 #1): binary controller entries
        carry the `.meta` GUID so convert_animations can scene-filter them.
        """
        ctrl = tmp_path / "Bin.controller"
        ctrl.write_bytes(b"\x00\x01\x02binary")
        meta = tmp_path / "Bin.controller.meta"
        meta.write_text("fileFormatVersion: 2\nguid: 12345abcdef0000000000000000000ff\n")
        out: list = []
        parse_controller_file(ctrl, unconverted_out=out)
        assert len(out) == 1
        assert out[0]["guid"] == "12345abcdef0000000000000000000ff"

    def test_scene_scoping_filters_unconverted_list(self, tmp_path: Path) -> None:
        """Phase 4.5b follow-up (Codex P2 #1): when scene-scoping is
        active, UNCONVERTED entries for controllers the run didn't
        emit must be dropped from `result.unconverted`.
        """
        from core.unity_types import ParsedScene
        assets = tmp_path / "Assets"
        assets.mkdir()
        # Controller A referenced by scene, but carries a 2D blend tree.
        ctrl_a = assets / "A.controller"
        ctrl_a.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: A
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Stand
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: StandBlend
              m_BlendType: 1
              m_BlendParameter: X
              m_Childs: []
            """))
        ctrl_a.with_suffix(".controller.meta").write_text(
            "fileFormatVersion: 2\nguid: " + "a" * 32 + "\n"
        )
        # Controller B also has a 2D blend tree but is NOT referenced.
        ctrl_b = assets / "B.controller"
        ctrl_b.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: B
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {fileID: 200}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {fileID: 300}
              m_DefaultState: {fileID: 300}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Fly
              m_Motion: {fileID: 400}
            --- !u!206 &400
            BlendTree:
              m_Name: FlyBlend
              m_BlendType: 1
              m_BlendParameter: X
              m_Childs: []
            """))
        ctrl_b.with_suffix(".controller.meta").write_text(
            "fileFormatVersion: 2\nguid: " + "b" * 32 + "\n"
        )
        # Scene only references A.
        scene = ParsedScene(
            scene_path=assets / "Level.unity",
            referenced_animator_controller_guids={"a" * 32},
        )

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(
            tmp_path, guid_index=guid_index, parsed_scenes=[scene],
        )

        # A's blend_tree entry stays; B's is filtered out.
        owners = {e.get("item", "").split("/", 1)[0] for e in result.unconverted
                  if e["category"] == "blend_tree"}
        assert "A" in owners, f"expected A in owners, got {owners} (unconverted={result.unconverted})"
        assert "B" not in owners, f"B should be filtered out, got {owners}"

    def test_generated_tween_script_has_policy_header(self) -> None:
        """Phase 4.5b: every inline TweenService script references the policy doc."""
        clip = AnimClip(
            name="Spin",
            duration=1.0,
            loop=True,
            sample_rate=30.0,
            curves=[
                AnimCurve(property_type="euler", path="Spinner",
                          keyframes=[
                              AnimKeyframe(time=0.0, value=(0, 0, 0)),
                              AnimKeyframe(time=1.0, value=(0, 360, 0)),
                          ]),
            ],
        )
        source = generate_tween_script(clip=clip)
        assert "inline-over-runtime-wrappers.md" in source

    def test_parsed_scenes_scene_prefix_applied(self, tmp_path: Path) -> None:
        """Scene-scoped names: tween scripts get prefixed by the scene stem."""
        from core.unity_types import ParsedScene
        assets = tmp_path / "Assets"
        assets.mkdir()
        # Transform-only clip (non-humanoid) so it emits an inline tween.
        anim = assets / "Spin.anim"
        anim.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &1
            AnimationClip:
              m_Name: Spin
              m_SampleRate: 30
              m_AnimationClipSettings:
                m_StartTime: 0
                m_StopTime: 1.0
                m_LoopTime: 1
              m_PositionCurves:
              - curve:
                  m_Curve:
                  - time: 0
                    value: {x: 0, y: 0, z: 0}
                path: Spinner
              m_RotationCurves: []
              m_EulerCurves: []
              m_ScaleCurves: []
            """))
        anim.with_suffix(".anim.meta").write_text(
            "fileFormatVersion: 2\nguid: " + "a" * 32 + "\n"
        )
        ctrl = assets / "Locomotion.controller"
        ctrl.write_text(textwrap.dedent(f"""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Locomotion
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {{fileID: 200}}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {{fileID: 300}}
              m_DefaultState: {{fileID: 300}}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Spin
              m_Motion:
                fileID: 7400000
                guid: {"a" * 32}
                type: 2
            """))
        ctrl.with_suffix(".controller.meta").write_text(
            "fileFormatVersion: 2\nguid: " + "b" * 32 + "\n"
        )
        scene_obj = ParsedScene(
            scene_path=assets / "Level1.unity",
            referenced_animator_controller_guids={"b" * 32},
        )

        from unity.guid_resolver import build_guid_index
        guid_index = build_guid_index(tmp_path)
        result = convert_animations(
            tmp_path, guid_index=guid_index, parsed_scenes=[scene_obj],
        )
        script_names = [name for name, _ in result.generated_scripts]
        assert any(n.startswith("Anim_Level1_Locomotion") for n in script_names), script_names


class TestPhase58PrefabControllerAggregation:
    """Phase 5.8: animator controller GUIDs referenced via prefab templates
    aggregate into the scene's referenced_animator_controller_guids set so
    scene-scoped emission activates even when Animators live exclusively in
    prefabs (the common case).
    """

    def test_prefab_template_extracts_animator_controller_guid(
        self, tmp_path: Path,
    ) -> None:
        """A .prefab containing an Animator records the controller GUID
        on its PrefabTemplate.
        """
        prefab_path = tmp_path / "Hero.prefab"
        prefab_path.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!1 &100
            GameObject:
              m_Name: Hero
              m_IsActive: 1
              m_Component:
              - component: {fileID: 101}
              - component: {fileID: 102}
            --- !u!4 &101
            Transform:
              m_GameObject: {fileID: 100}
              m_LocalPosition: {x: 0, y: 0, z: 0}
              m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}
              m_LocalScale: {x: 1, y: 1, z: 1}
              m_Father: {fileID: 0}
            --- !u!95 &102
            Animator:
              m_GameObject: {fileID: 100}
              m_Controller: {fileID: 9100000, guid: """ + "c" * 32 + """, type: 2}
            """))
        from unity.prefab_parser import _parse_single_prefab
        template = _parse_single_prefab(prefab_path)
        assert "c" * 32 in template.referenced_animator_controller_guids

    def test_aggregate_unions_prefab_refs_into_scene(self) -> None:
        """aggregate_prefab_controller_refs() walks a scene's prefab_instances
        and unions their controller GUIDs into the scene's set.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.prefab_parser import aggregate_prefab_controller_refs

        prefab = PrefabTemplate(
            prefab_path=Path("/fake/Hero.prefab"),
            name="Hero",
            referenced_animator_controller_guids={"c" * 32, "d" * 32},
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Hero": prefab},
            by_guid={"a" * 32: prefab},
        )
        scene = ParsedScene(
            scene_path=Path("Level1.unity"),
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="a" * 32,
                source_prefab_file_id="100100000",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )

        added = aggregate_prefab_controller_refs(scene, library)
        assert added == 2
        assert scene.referenced_animator_controller_guids == {"c" * 32, "d" * 32}

    def test_aggregate_skips_unknown_prefab_guid(self) -> None:
        """When a scene's PrefabInstance points to a prefab not in the
        library (e.g. broken reference), the helper silently skips it.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
        )
        from unity.prefab_parser import aggregate_prefab_controller_refs

        scene = ParsedScene(
            scene_path=Path("Level1.unity"),
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="missing" + "0" * 26,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        added = aggregate_prefab_controller_refs(scene, PrefabLibrary())
        assert added == 0
        assert not scene.referenced_animator_controller_guids

    def test_aggregate_no_prefab_instances_returns_zero(self) -> None:
        """A scene with zero PrefabInstance documents is a no-op."""
        from core.unity_types import ParsedScene, PrefabLibrary
        from unity.prefab_parser import aggregate_prefab_controller_refs

        scene = ParsedScene(scene_path=Path("Empty.unity"))
        added = aggregate_prefab_controller_refs(scene, PrefabLibrary())
        assert added == 0

    def test_instance_override_of_controller_aggregates(self) -> None:
        """Codex P1 fix: when a scene's PrefabInstance overrides
        Animator.m_Controller per-instance, the override GUID must be
        unioned into the scene's controller set in addition to (or
        instead of) the prefab template's base controller.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.prefab_parser import aggregate_prefab_controller_refs

        prefab = PrefabTemplate(
            prefab_path=Path("/fake/Hero.prefab"),
            name="Hero",
            referenced_animator_controller_guids={"base" + "0" * 28},
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Hero": prefab},
            by_guid={"hero" + "0" * 28: prefab},
        )
        # Scene instance overrides m_Controller with a different GUID.
        scene = ParsedScene(
            scene_path=Path("Level1.unity"),
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="hero" + "0" * 28,
                source_prefab_file_id="100100000",
                transform_parent_file_id="0",
                modifications=[
                    {
                        "target": {"fileID": 102},
                        "propertyPath": "m_Controller",
                        "value": "",
                        "objectReference": {"guid": "ovrd" + "0" * 28},
                    },
                ],
            )],
        )

        added = aggregate_prefab_controller_refs(scene, library)
        # Both base + override are recorded.
        assert "base" + "0" * 28 in scene.referenced_animator_controller_guids
        assert "ovrd" + "0" * 28 in scene.referenced_animator_controller_guids
        assert added == 2

    def test_instance_override_without_template_still_aggregates(self) -> None:
        """A scene-level override on a missing-from-library prefab still
        records the override GUID so the new controller routes correctly.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
        )
        from unity.prefab_parser import aggregate_prefab_controller_refs

        scene = ParsedScene(
            scene_path=Path("Level1.unity"),
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="missing" + "0" * 25,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[
                    {
                        "target": {"fileID": 102},
                        "propertyPath": "m_Controller",
                        "value": "",
                        "objectReference": {"guid": "ovrd" + "0" * 28},
                    },
                ],
            )],
        )

        added = aggregate_prefab_controller_refs(scene, PrefabLibrary())
        assert "ovrd" + "0" * 28 in scene.referenced_animator_controller_guids
        assert added == 1

    def test_variant_inherits_source_controller_refs(self) -> None:
        """A prefab variant with no controller overrides inherits its
        source's animator controller GUIDs through the merged component
        graph (variant chain deep-copies the source's node tree).
        """
        from core.unity_types import (
            PrefabComponent,
            PrefabNode,
            PrefabTemplate,
        )
        from unity.prefab_parser import _resolve_variant_chain

        source_root = PrefabNode(
            name="Source",
            file_id="1",
            active=True,
            components=[
                PrefabComponent(
                    component_type="Animator",
                    file_id="2",
                    properties={
                        "m_Controller": {
                            "fileID": 9100000,
                            "guid": "c" * 32,
                            "type": 2,
                        },
                    },
                ),
            ],
        )
        source = PrefabTemplate(
            prefab_path=Path("/fake/Source.prefab"),
            name="Source",
            root=source_root,
            all_nodes={"1": source_root},
            referenced_animator_controller_guids={"c" * 32},
            variant_resolved=True,
        )
        variant = PrefabTemplate(
            prefab_path=Path("/fake/Variant.prefab"),
            name="Variant",
            root=PrefabNode(name="Variant", file_id="2", active=True),
            all_nodes={"2": PrefabNode(name="Variant", file_id="2", active=True)},
            is_variant=True,
            source_prefab_guid="src" + "0" * 29,
        )
        by_guid = {"src" + "0" * 29: source}
        _resolve_variant_chain(variant, by_guid)
        assert "c" * 32 in variant.referenced_animator_controller_guids


class TestPhase59PrefabScopedTweenScripts:
    """Phase 5.9: when an animator controller belongs to a prefab template,
    convert_animations emits one tween script per prefab template (not per
    scene-instance), with the prefab name as the script-name scope.
    """

    def _build_transform_only_controller_project(
        self, tmp_path: Path,
    ) -> tuple[Path, str, str]:
        """Lay out a project with a transform-only .anim, a controller that
        references it, and return (project_root, controller_guid, anim_guid).
        """
        assets = tmp_path / "Assets"
        assets.mkdir()
        anim = assets / "Spin.anim"
        anim.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &1
            AnimationClip:
              m_Name: Spin
              m_SampleRate: 30
              m_AnimationClipSettings:
                m_StartTime: 0
                m_StopTime: 1.0
                m_LoopTime: 1
              m_PositionCurves: []
              m_RotationCurves: []
              m_EulerCurves:
              - curve:
                  m_Curve:
                  - time: 0
                    value: {x: 0, y: 0, z: 0}
                  - time: 1
                    value: {x: 0, y: 360, z: 0}
                path: Wheel
              m_ScaleCurves: []
            """))
        anim_guid = "a" * 32
        anim.with_suffix(".anim.meta").write_text(
            f"fileFormatVersion: 2\nguid: {anim_guid}\n"
        )
        ctrl = assets / "Wheel.controller"
        ctrl.write_text(textwrap.dedent(f"""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!91 &9100000
            AnimatorController:
              m_Name: Wheel
              m_AnimatorLayers:
              - m_Name: Base Layer
                m_StateMachine: {{fileID: 200}}
            --- !u!1107 &200
            AnimatorStateMachine:
              m_ChildStates:
              - m_State: {{fileID: 300}}
              m_DefaultState: {{fileID: 300}}
            --- !u!1102 &300
            AnimatorState:
              m_Name: Spin
              m_Motion:
                fileID: 7400000
                guid: {anim_guid}
                type: 2
            """))
        ctrl_guid = "b" * 32
        ctrl.with_suffix(".controller.meta").write_text(
            f"fileFormatVersion: 2\nguid: {ctrl_guid}\n"
        )
        return tmp_path, ctrl_guid, anim_guid

    def test_prefab_scope_used_when_controller_lives_in_prefab(
        self, tmp_path: Path,
    ) -> None:
        """Prefab-referenced controller emits a tween script with the
        prefab name as the scope prefix.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index
        from unity.prefab_parser import aggregate_prefab_controller_refs

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Vehicle.prefab",
            name="Vehicle",
            referenced_animator_controller_guids={ctrl_guid},
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Vehicle": prefab},
            by_guid={"vehicle" + "0" * 25: prefab},
        )
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="vehicle" + "0" * 25,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        # Aggregate populates the scene set so scene filtering activates.
        aggregate_prefab_controller_refs(scene, library)

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        # Tween script scoped to prefab name, not scene name.
        names = [name for name, _ in result.generated_scripts]
        assert any(n == "Anim_Vehicle_Wheel_Spin" for n in names), names
        # Scene scope must NOT be present when prefab scope is in effect.
        assert not any(n == "Anim_Level1_Wheel_Spin" for n in names), names
        # script_scopes carries the prefab name so the pipeline can
        # reparent the script under ReplicatedStorage.Templates.<Prefab>
        # — the scene-scoped path must NOT populate this map (asserted
        # in a sibling test below).
        assert result.script_scopes.get("Anim_Vehicle_Wheel_Spin") == "Vehicle", (
            f"expected prefab-scoped script in script_scopes, got "
            f"{result.script_scopes}"
        )

    def test_prefab_scoped_script_targets_script_parent_not_workspace(
        self, tmp_path: Path,
    ) -> None:
        """Prefab-scoped scripts must bind to ``script.Parent`` (the cloned
        model), not to ``workspace:FindFirstChild(name, true)``. Without
        this, two clones of the same prefab template each carry their own
        script, but every clone's script binds to whichever clone
        FindFirstChild returns first — only one clone animates correctly.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index
        from unity.prefab_parser import aggregate_prefab_controller_refs

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Vehicle.prefab",
            name="Vehicle",
            referenced_animator_controller_guids={ctrl_guid},
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Vehicle": prefab},
            by_guid={"vehicle" + "0" * 25: prefab},
        )
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="vehicle" + "0" * 25,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        aggregate_prefab_controller_refs(scene, library)

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        prefab_source = next(
            source for name, source in result.generated_scripts
            if name == "Anim_Vehicle_Wheel_Spin"
        )
        # Smart binding: script.Parent (clone) wins, with a workspace
        # fallback only for the global flat-list copy that drives
        # scene-baked instances. Both branches must be present.
        assert "script.Parent" in prefab_source, (
            "expected prefab-scoped script to bind via script.Parent so "
            "each clone animates its own copy; full source:\n" + prefab_source
        )
        assert "workspace:FindFirstChild" in prefab_source, (
            "expected prefab-scoped script to keep a workspace fallback for "
            "scene-baked instances; full source:\n" + prefab_source
        )
        # The branch ordering matters: script.Parent first, fall back to
        # workspace search. Otherwise multi-clone setups race the global
        # search ahead of the per-clone binding.
        parent_idx = prefab_source.index("script.Parent")
        ws_idx = prefab_source.index("workspace:FindFirstChild")
        assert parent_idx < ws_idx, (
            "script.Parent branch must precede the workspace fallback so "
            "clones bind to themselves before falling through; full source:\n"
            + prefab_source
        )

    def test_scene_scoped_script_keeps_workspace_lookup(
        self, tmp_path: Path,
    ) -> None:
        """Scene-scoped scripts (no prefab template to live in) keep the
        legacy workspace lookup. Switching them to ``script.Parent`` would
        miss the target since these scripts live in a global container."""
        from core.unity_types import ParsedScene
        from unity.guid_resolver import build_guid_index

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            referenced_animator_controller_guids={ctrl_guid},
        )
        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
        )
        scene_source = next(
            source for name, source in result.generated_scripts
            if name == "Anim_Level1_Wheel_Spin"
        )
        assert "workspace:FindFirstChild" in scene_source, (
            "scene-scoped scripts must keep the workspace lookup; "
            "full source:\n" + scene_source
        )

    def test_scene_scoped_emission_does_not_set_script_scopes(
        self, tmp_path: Path,
    ) -> None:
        """When emission falls back to scene scope (controller referenced
        directly by a scene's GameObject — not via a prefab), script_scopes
        stays empty so the pipeline keeps those scripts in the place's flat
        list. Reparenting a scene-scoped script under a prefab template
        would be wrong: there is no template for the script to live in."""
        from core.unity_types import ParsedScene
        from unity.guid_resolver import build_guid_index

        project, _ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        # Scene that directly references the controller (no prefab).
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            referenced_animator_controller_guids={_ctrl_guid},
        )
        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
        )
        names = [name for name, _ in result.generated_scripts]
        assert any(n == "Anim_Level1_Wheel_Spin" for n in names), names
        assert result.script_scopes == {}, (
            f"expected empty script_scopes for scene-scoped emission, "
            f"got {result.script_scopes}"
        )

    def test_multiple_prefab_instances_do_not_duplicate_script(
        self, tmp_path: Path,
    ) -> None:
        """Acceptance: a fixture with a prefab containing transform-only
        .anim produces ONE tween script per prefab template even when the
        prefab is instantiated from multiple scenes.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index
        from unity.prefab_parser import aggregate_prefab_controller_refs

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Vehicle.prefab",
            name="Vehicle",
            referenced_animator_controller_guids={ctrl_guid},
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Vehicle": prefab},
            by_guid={"vehicle" + "0" * 25: prefab},
        )
        instance = PrefabInstanceData(
            file_id="500",
            source_prefab_guid="vehicle" + "0" * 25,
            source_prefab_file_id="0",
            transform_parent_file_id="0",
            modifications=[],
        )
        scene_a = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[instance],
        )
        scene_b = ParsedScene(
            scene_path=project / "Assets" / "Level2.unity",
            prefab_instances=[instance],
        )
        for s in (scene_a, scene_b):
            aggregate_prefab_controller_refs(s, library)

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene_a, scene_b],
            prefab_library=library,
        )
        names = [name for name, _ in result.generated_scripts]
        # Exactly one script — prefab-scoped, not duplicated per scene.
        scripts_for_clip = [n for n in names if n.endswith("_Wheel_Spin")]
        assert scripts_for_clip == ["Anim_Vehicle_Wheel_Spin"], scripts_for_clip

    def test_scene_only_controller_keeps_scene_scope(
        self, tmp_path: Path,
    ) -> None:
        """Backward compat: a controller referenced directly by a scene
        (not via any prefab) still gets a scene-scoped script name.
        """
        from core.unity_types import ParsedScene, PrefabLibrary
        from unity.guid_resolver import build_guid_index

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            referenced_animator_controller_guids={ctrl_guid},
        )
        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=PrefabLibrary(),
        )
        names = [name for name, _ in result.generated_scripts]
        assert any(n == "Anim_Level1_Wheel_Spin" for n in names), names

    def test_uninstantiated_prefab_not_emitted_when_scene_filtering(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 fix: when scene filtering is active, only prefabs
        actually instantiated by a parsed scene contribute prefab scopes —
        prefabs in the library that no scene references must NOT force-
        emit their controllers.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index
        from unity.prefab_parser import aggregate_prefab_controller_refs

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        # Two prefabs both reference the controller, but only one is
        # instantiated in any scene.
        used_prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Used.prefab",
            name="Used",
            referenced_animator_controller_guids={ctrl_guid},
        )
        unused_prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Unused.prefab",
            name="Unused",
            referenced_animator_controller_guids={ctrl_guid},
        )
        library = PrefabLibrary(
            prefabs=[used_prefab, unused_prefab],
            by_name={"Used": used_prefab, "Unused": unused_prefab},
            by_guid={
                "used" + "0" * 28: used_prefab,
                "unused" + "0" * 26: unused_prefab,
            },
        )
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="used" + "0" * 28,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        aggregate_prefab_controller_refs(scene, library)

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        names = [name for name, _ in result.generated_scripts]
        # Used prefab gets its scope; unused prefab must not.
        assert any(n == "Anim_Used_Wheel_Spin" for n in names), names
        assert not any(n == "Anim_Unused_Wheel_Spin" for n in names), names

    def test_uninstantiated_prefab_filtered_even_without_pre_aggregation(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 #2 fix: when parsed_scenes is supplied, the prefab
        in-scope filter activates regardless of whether the scene's
        referenced_animator_controller_guids set has been pre-aggregated.
        A direct caller of convert_animations that skips
        aggregate_prefab_controller_refs() should still get correctly
        scope-restricted prefab emission.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        used_prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Used.prefab",
            name="Used",
            referenced_animator_controller_guids={ctrl_guid},
        )
        unused_prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Unused.prefab",
            name="Unused",
            referenced_animator_controller_guids={ctrl_guid},
        )
        library = PrefabLibrary(
            prefabs=[used_prefab, unused_prefab],
            by_name={"Used": used_prefab, "Unused": unused_prefab},
            by_guid={
                "used" + "0" * 28: used_prefab,
                "unused" + "0" * 26: unused_prefab,
            },
        )
        # Scene has prefab_instance pointing at Used, but its
        # referenced_animator_controller_guids is left EMPTY (no pre-
        # aggregation). The prefab-scope filter must still kick in.
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="used" + "0" * 28,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        names = [name for name, _ in result.generated_scripts]
        # Only the instantiated prefab gets a scope; Unused is filtered.
        assert any(n == "Anim_Used_Wheel_Spin" for n in names), names
        assert not any(n == "Anim_Unused_Wheel_Spin" for n in names), names

    def test_variant_override_of_controller_uses_new_guid(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 fix (round 1): when a prefab variant overrides an
        Animator's m_Controller, the merged template's
        referenced_animator_controller_guids rebuilds from the modified
        component graph — base controller GUID is dropped and the override
        GUID takes its place.
        """
        from core.unity_types import (
            PrefabComponent,
            PrefabNode,
            PrefabTemplate,
        )
        from unity.prefab_parser import (
            _resolve_variant_chain,
            _collect_animator_controller_guids,
        )

        # Source prefab has Animator pointing at controller GUID c*32.
        source_root = PrefabNode(
            name="Hero",
            file_id="100",
            active=True,
            components=[
                PrefabComponent(
                    component_type="Animator",
                    file_id="102",
                    properties={
                        "m_Controller": {
                            "fileID": 9100000,
                            "guid": "c" * 32,
                            "type": 2,
                        },
                    },
                ),
            ],
        )
        source = PrefabTemplate(
            prefab_path=Path("/fake/Hero.prefab"),
            name="Hero",
            root=source_root,
            all_nodes={"100": source_root},
            referenced_animator_controller_guids={"c" * 32},
            variant_resolved=True,
        )
        # Variant overrides m_Controller.guid to "d"*32.
        variant_root = PrefabNode(name="HeroVariant", file_id="V100", active=True)
        variant = PrefabTemplate(
            prefab_path=Path("/fake/HeroVariant.prefab"),
            name="HeroVariant",
            root=variant_root,
            all_nodes={"V100": variant_root},
            is_variant=True,
            source_prefab_guid="hero" + "0" * 28,
            variant_modifications=[
                {
                    "target": {"fileID": "102"},
                    "propertyPath": "m_Controller",
                    "value": "",
                    "objectReference": {"fileID": 9100000, "guid": "d" * 32, "type": 2},
                },
            ],
        )
        by_guid = {"hero" + "0" * 28: source}
        _resolve_variant_chain(variant, by_guid)

        # Merged tree's Animator points at the override GUID.
        merged_refs = _collect_animator_controller_guids(variant.all_nodes)
        assert "d" * 32 in merged_refs
        # Base controller is no longer referenced (no variant-added Animator
        # would have it; the override replaces the source's pointer).
        assert "c" * 32 not in merged_refs

    def test_scene_with_no_refs_or_instances_skips_all_controllers(
        self, tmp_path: Path,
    ) -> None:
        """Passing ``parsed_scenes=[empty_scene]`` with no prefab_library
        is itself a request for scope-restricted output. A scene that
        references no controllers and instantiates no prefabs should
        produce zero animation scripts for the project's controllers,
        not fall through to project-wide emission.
        """
        from core.unity_types import ParsedScene
        from unity.guid_resolver import build_guid_index

        project, _, _ = self._build_transform_only_controller_project(tmp_path)
        empty_scene = ParsedScene(
            scene_path=project / "Assets" / "Empty.unity",
            prefab_instances=[],
        )

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[empty_scene],
            # Deliberately no prefab_library — the scope intent is the
            # parsed_scenes argument itself, not a prefab pre-aggregation.
        )
        assert "Wheel" in result.routing
        assert result.routing["Wheel"]["__controller__"]["target"] == "skipped"
        names = [name for name, _ in result.generated_scripts]
        assert not any("Wheel" in n for n in names), names

    def test_unrelated_controller_skipped_when_scene_has_only_prefab_refs(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 fix (round 5): when scene filtering is active via
        parsed_scenes + prefab_library (the common prefab-only case),
        controllers not referenced by any instantiated prefab are
        SKIPPED instead of falling through to default_scopes (unscoped).
        Prevents leakage of unrelated controllers from other scenes.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        # Library has a prefab that DOESN'T reference this controller.
        unrelated_prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Other.prefab",
            name="Other",
            referenced_animator_controller_guids=set(),
        )
        library = PrefabLibrary(
            prefabs=[unrelated_prefab],
            by_name={"Other": unrelated_prefab},
            by_guid={"other" + "0" * 27: unrelated_prefab},
        )
        # Scene has zero direct controller refs and instantiates the
        # unrelated prefab (which doesn't have the controller either).
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="other" + "0" * 27,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        # Controller is skipped — no scripts emit for it.
        assert "Wheel" in result.routing
        assert result.routing["Wheel"]["__controller__"]["target"] == "skipped"
        names = [name for name, _ in result.generated_scripts]
        assert not any("Wheel" in n for n in names), names

    def test_instance_override_routes_to_prefab_scope(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 fix (round 3): when a scene's PrefabInstance overrides
        Animator.m_Controller per-instance, the override controller emits
        under the prefab template's scope (not the scene's), preserving
        the one-script-per-prefab dedupe.
        """
        from core.unity_types import (
            ParsedScene,
            PrefabInstanceData,
            PrefabLibrary,
            PrefabTemplate,
        )
        from unity.guid_resolver import build_guid_index
        from unity.prefab_parser import aggregate_prefab_controller_refs

        project, ctrl_guid, _ = self._build_transform_only_controller_project(tmp_path)
        # The prefab template doesn't reference this controller; only the
        # scene-level override does.
        prefab = PrefabTemplate(
            prefab_path=project / "Assets" / "Vehicle.prefab",
            name="Vehicle",
            referenced_animator_controller_guids=set(),
        )
        library = PrefabLibrary(
            prefabs=[prefab],
            by_name={"Vehicle": prefab},
            by_guid={"vehicle" + "0" * 25: prefab},
        )
        scene = ParsedScene(
            scene_path=project / "Assets" / "Level1.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid="vehicle" + "0" * 25,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[
                    {
                        "target": {"fileID": 102},
                        "propertyPath": "m_Controller",
                        "value": "",
                        "objectReference": {"guid": ctrl_guid},
                    },
                ],
            )],
        )
        # Aggregate populates the scene set with the override GUID.
        aggregate_prefab_controller_refs(scene, library)

        guid_index = build_guid_index(project)
        result = convert_animations(
            project,
            guid_index=guid_index,
            parsed_scenes=[scene],
            prefab_library=library,
        )
        names = [name for name, _ in result.generated_scripts]
        # Override routes to the prefab scope, not the scene scope.
        assert any(n == "Anim_Vehicle_Wheel_Spin" for n in names), names
        assert not any(n == "Anim_Level1_Wheel_Spin" for n in names), names

    def test_variant_added_animator_keeps_controller_ref(self) -> None:
        """Codex P2 #3 fix: when a variant prefab adds its OWN Animator (the
        component lives outside the source's tree), the variant's pre-merge
        referenced_animator_controller_guids must survive variant chain
        resolution. Without the union, recompute-from-merged would drop it.
        """
        from core.unity_types import (
            PrefabComponent,
            PrefabNode,
            PrefabTemplate,
        )
        from unity.prefab_parser import _resolve_variant_chain

        # Source has no Animator components.
        source_root = PrefabNode(name="Source", file_id="100", active=True)
        source = PrefabTemplate(
            prefab_path=Path("/fake/Source.prefab"),
            name="Source",
            root=source_root,
            all_nodes={"100": source_root},
            referenced_animator_controller_guids=set(),
            variant_resolved=True,
        )
        # Variant declares its own Animator with controller "v"*32.
        # Simulate _parse_single_prefab populating
        # template.referenced_animator_controller_guids before the variant
        # chain resolves.
        variant_root = PrefabNode(name="Variant", file_id="V100", active=True)
        variant = PrefabTemplate(
            prefab_path=Path("/fake/Variant.prefab"),
            name="Variant",
            root=variant_root,
            all_nodes={"V100": variant_root},
            referenced_animator_controller_guids={"v" * 32},
            is_variant=True,
            source_prefab_guid="src" + "0" * 29,
        )
        by_guid = {"src" + "0" * 29: source}
        _resolve_variant_chain(variant, by_guid)
        # Variant's controller GUID survives the merge.
        assert "v" * 32 in variant.referenced_animator_controller_guids
        """Codex P2 fix: when a prefab variant overrides an Animator's
        m_Controller, the merged template's referenced_animator_controller_guids
        rebuilds from the modified component graph — base controller GUID
        is dropped and the override GUID takes its place.
        """
        from core.unity_types import (
            PrefabNode,
            PrefabTemplate,
        )
        from unity.prefab_parser import (
            _resolve_variant_chain,
            _collect_animator_controller_guids,
        )

        # Source prefab has Animator pointing at controller GUID c*32.
        source_root = PrefabNode(
            name="Hero",
            file_id="100",
            active=True,
            components=[
                PrefabComponent(
                    component_type="Animator",
                    file_id="102",
                    properties={
                        "m_Controller": {
                            "fileID": 9100000,
                            "guid": "c" * 32,
                            "type": 2,
                        },
                    },
                ),
            ],
        )
        source = PrefabTemplate(
            prefab_path=Path("/fake/Hero.prefab"),
            name="Hero",
            root=source_root,
            all_nodes={"100": source_root},
            referenced_animator_controller_guids={"c" * 32},
            variant_resolved=True,
        )
        # Variant overrides m_Controller.guid to "d"*32.
        variant_root = PrefabNode(name="HeroVariant", file_id="V100", active=True)
        variant = PrefabTemplate(
            prefab_path=Path("/fake/HeroVariant.prefab"),
            name="HeroVariant",
            root=variant_root,
            all_nodes={"V100": variant_root},
            is_variant=True,
            source_prefab_guid="hero" + "0" * 28,
            variant_modifications=[
                {
                    "target": {"fileID": "102"},
                    "propertyPath": "m_Controller",
                    "value": "",
                    "objectReference": {"fileID": 9100000, "guid": "d" * 32, "type": 2},
                },
            ],
        )
        by_guid = {"hero" + "0" * 28: source}
        _resolve_variant_chain(variant, by_guid)

        # Merged tree's Animator points at the override GUID.
        merged_refs = _collect_animator_controller_guids(variant.all_nodes)
        assert "d" * 32 in merged_refs
        assert variant.referenced_animator_controller_guids == merged_refs
        # Base controller is no longer referenced.
        assert "c" * 32 not in variant.referenced_animator_controller_guids

