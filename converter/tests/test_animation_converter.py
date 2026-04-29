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
    AnimCondition,
    AnimCurve,
    AnimKeyframe,
    AnimParameter,
    AnimState,
    AnimTransition,
    AnimatorController,
    AnimationConversionResult,
    BlendTree,
    BlendTreeEntry,
    parse_anim_file,
    parse_controller_file,
    simplify_keyframes,
    generate_tween_script,
    discover_animations,
    convert_animations,
    generate_state_machine_script,
    export_controller_json,
    export_clip_keyframes,
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
        # Should contain the Y offset of 4
        assert "4.0000" in luau

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
        assert "while true do" in luau  # Looping animation

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


class TestStateMachineGeneration:
    """Tests for unified state machine script generation."""

    def _make_controller_with_transitions(self) -> tuple[AnimatorController, dict[str, AnimClip]]:
        """Create a controller with Idle→Walk→Run states and transitions."""
        idle = AnimState(
            name="Idle", file_id="100",
            clip_guid="guid_idle", speed=1.0,
            transitions=[
                AnimTransition(
                    name="IdleToWalk", dst_state_file_id="200",
                    conditions=[AnimCondition(parameter="Speed", mode=3, threshold=0.1)],
                ),
            ],
        )
        walk = AnimState(
            name="Walk", file_id="200",
            clip_guid="guid_walk", speed=1.0,
            transitions=[
                AnimTransition(
                    name="WalkToRun", dst_state_file_id="300",
                    conditions=[AnimCondition(parameter="Speed", mode=3, threshold=5.0)],
                ),
                AnimTransition(
                    name="WalkToIdle", dst_state_file_id="100",
                    conditions=[AnimCondition(parameter="Speed", mode=4, threshold=0.1)],
                ),
            ],
        )
        run = AnimState(
            name="Run", file_id="300",
            clip_guid="guid_run", speed=1.5,
            transitions=[
                AnimTransition(
                    name="RunToWalk", dst_state_file_id="200",
                    conditions=[AnimCondition(parameter="Speed", mode=4, threshold=5.0)],
                ),
            ],
        )
        ctrl = AnimatorController(
            name="CharacterAnimator",
            parameters=[
                AnimParameter(name="Speed", param_type=1, default_float=0.0),
                AnimParameter(name="IsGrounded", param_type=4, default_bool=True),
            ],
            states=[idle, walk, run],
            default_state_file_id="100",
        )
        clips = {
            "guid_idle": AnimClip(name="Idle", duration=1.0, loop=True, sample_rate=30),
            "guid_walk": AnimClip(name="Walk", duration=0.8, loop=True, sample_rate=30),
            "guid_run": AnimClip(name="Run", duration=0.5, loop=True, sample_rate=30),
        }
        return ctrl, clips

    def test_state_machine_generates_output(self) -> None:
        """State machine script should be generated for controllers with transitions."""
        ctrl, clips = self._make_controller_with_transitions()
        source = generate_state_machine_script(ctrl, clips, "Player")
        assert source
        assert "State Machine" in source
        assert "CharacterAnimator" in source

    def test_state_machine_has_all_states(self) -> None:
        """All states should appear in the generated script."""
        ctrl, clips = self._make_controller_with_transitions()
        source = generate_state_machine_script(ctrl, clips)
        assert "Idle" in source
        assert "Walk" in source
        assert "Run" in source

    def test_state_machine_initializes_parameters(self) -> None:
        """Parameters should be initialized as attributes."""
        ctrl, clips = self._make_controller_with_transitions()
        source = generate_state_machine_script(ctrl, clips)
        assert 'SetAttribute("Speed"' in source
        assert 'SetAttribute("IsGrounded"' in source

    def test_state_machine_has_transitions(self) -> None:
        """Transition conditions should appear in the script."""
        ctrl, clips = self._make_controller_with_transitions()
        source = generate_state_machine_script(ctrl, clips)
        assert 'GetAttribute("Speed")' in source
        assert "> 0.1" in source or "> 5" in source

    def test_state_machine_default_state(self) -> None:
        """Default state should be set to the controller's default."""
        ctrl, clips = self._make_controller_with_transitions()
        source = generate_state_machine_script(ctrl, clips)
        assert 'currentState = "Idle"' in source

    def test_state_machine_trigger_reset(self) -> None:
        """Trigger parameters should be reset after firing."""
        ctrl = AnimatorController(
            name="DoorCtrl",
            parameters=[AnimParameter(name="Open", param_type=9)],
            states=[
                AnimState(name="Closed", file_id="1", clip_guid="g1", transitions=[
                    AnimTransition(name="t", dst_state_file_id="2",
                                   conditions=[AnimCondition(parameter="Open", mode=1)]),
                ]),
                AnimState(name="Opened", file_id="2", clip_guid="g2"),
            ],
            default_state_file_id="1",
        )
        clips = {
            "g1": AnimClip(name="Close", duration=0.5, loop=False, sample_rate=30),
            "g2": AnimClip(name="Open", duration=0.5, loop=False, sample_rate=30),
        }
        source = generate_state_machine_script(ctrl, clips)
        # Trigger should be reset to false after transition
        assert 'SetAttribute("Open", false)' in source


# ---------------------------------------------------------------------------
# Animation data export (controller JSON + keyframe data)
# ---------------------------------------------------------------------------

class TestAnimationDataExport:

    def _make_controller_with_clip(self):
        clip = AnimClip(
            name="Walk",
            duration=1.0,
            loop=True,
            sample_rate=30,
            curves=[
                AnimCurve(
                    path="Hips",
                    property_type="position",
                    keyframes=[
                        AnimKeyframe(time=0.0, value=(0.0, 0.0, 0.0)),
                        AnimKeyframe(time=1.0, value=(1.0, 2.0, 3.0)),
                    ],
                ),
            ],
        )
        ctrl = AnimatorController(
            name="HumanoidCtrl",
            states=[
                AnimState(name="Idle", file_id="1", clip_guid="g1"),
                AnimState(name="Walking", file_id="2", clip_guid="g2",
                          transitions=[
                              AnimTransition(
                                  name="t", dst_state_file_id="1",
                                  conditions=[AnimCondition(parameter="Speed", mode=4, threshold=0.1)],
                              ),
                          ]),
            ],
            parameters=[
                AnimParameter(name="Speed", param_type=1, default_float=0.0),
            ],
            default_state_file_id="1",
        )
        return ctrl, clip

    def test_export_controller_json(self):
        ctrl, _ = self._make_controller_with_clip()
        data = export_controller_json(ctrl)
        assert data["name"] == "HumanoidCtrl"
        assert len(data["states"]) == 2
        assert data["defaultState"] == "Idle"
        assert data["parameters"][0]["name"] == "Speed"
        assert data["parameters"][0]["type"] == "Float"
        walking = [s for s in data["states"] if s["name"] == "Walking"][0]
        assert walking["transitions"][0]["destination"] == "Idle"

    def test_export_condition_modes(self):
        """All 6 condition modes the animator_runtime supports are mapped."""
        mode_map = {1: "If", 2: "IfNot", 3: "Greater", 4: "Less", 6: "Equals", 7: "NotEqual"}
        conditions = []
        for mode_int, mode_str in mode_map.items():
            conditions.append(AnimCondition(parameter="p", mode=mode_int, threshold=0.5))
        ctrl = AnimatorController(
            name="C", states=[
                AnimState(name="A", file_id="1", clip_guid="g1"),
                AnimState(name="B", file_id="2", clip_guid="g2",
                          transitions=[AnimTransition(name="t", dst_state_file_id="1", conditions=conditions)]),
            ],
            parameters=[AnimParameter(name="p", param_type=1, default_float=0.0)],
            default_state_file_id="1",
        )
        data = export_controller_json(ctrl)
        exported_modes = {c["mode"] for s in data["states"] for t in s["transitions"] for c in t["conditions"]}
        assert exported_modes == set(mode_map.values())

    def test_export_transition_fields_match_runtime(self):
        """Transition fields include everything animator_runtime reads."""
        ctrl, _ = self._make_controller_with_clip()
        data = export_controller_json(ctrl)
        walking = [s for s in data["states"] if s["name"] == "Walking"][0]
        tr = walking["transitions"][0]
        for field in ("destination", "conditions", "duration", "hasExitTime", "exitTime"):
            assert field in tr, f"missing transition field: {field}"

    def test_export_parameter_types(self):
        """All Unity parameter types map to strings the runtime supports."""
        ctrl = AnimatorController(
            name="C", states=[AnimState(name="S", file_id="1", clip_guid="g")],
            parameters=[
                AnimParameter(name="f", param_type=1, default_float=1.5),
                AnimParameter(name="i", param_type=3, default_int=7),
                AnimParameter(name="b", param_type=4, default_bool=True),
                AnimParameter(name="t", param_type=9),
            ],
            default_state_file_id="1",
        )
        data = export_controller_json(ctrl)
        by_name = {p["name"]: p for p in data["parameters"]}
        assert by_name["f"]["type"] == "Float" and by_name["f"]["defaultValue"] == 1.5
        assert by_name["i"]["type"] == "Int" and by_name["i"]["defaultValue"] == 7
        assert by_name["b"]["type"] == "Bool" and by_name["b"]["defaultValue"] is True
        assert by_name["t"]["type"] == "Trigger"

    def test_export_clip_keyframes(self):
        _, clip = self._make_controller_with_clip()
        data = export_clip_keyframes(clip)
        assert data["duration"] == 1.0
        assert "Hips" in data["bones"]
        frames = data["bones"]["Hips"]
        assert len(frames) == 2
        assert frames[0]["time"] == 0.0
        assert frames[1]["cf"]["x"] == 1.0
        # Z should be negated (Unity -> Roblox)
        assert frames[1]["cf"]["z"] == -3.0

    def test_animation_data_modules_generated(self):
        """convert_animations populates animation_data_modules for controllers with clips."""
        import json
        import tempfile, os

        ctrl, clip = self._make_controller_with_clip()

        # Create a minimal project structure with an .anim and .controller
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "Assets" / "Animations"
            assets.mkdir(parents=True)

            # Write a minimal .anim file
            anim_yaml = textwrap.dedent(f"""\
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
                        value: {{x: 0, y: 0, z: 0}}
                        inSlope: {{x: 0, y: 0, z: 0}}
                        outSlope: {{x: 0, y: 0, z: 0}}
                      - serializedVersion: 3
                        time: 1
                        value: {{x: 1, y: 2, z: 3}}
                        inSlope: {{x: 0, y: 0, z: 0}}
                        outSlope: {{x: 0, y: 0, z: 0}}
                    path: Hips
                  m_RotationCurves: []
                  m_EulerCurves: []
                  m_ScaleCurves: []
                  m_AnimationClipSettings:
                    serializedVersion: 2
                    m_LoopTime: 1
                    m_StopTime: 1
            """)
            anim_path = assets / "Walk.anim"
            anim_path.write_text(anim_yaml)

            # Write a .anim.meta to give it a GUID
            meta = textwrap.dedent("""\
                fileFormatVersion: 2
                guid: abcd1234abcd1234abcd1234abcd1234
            """)
            (assets / "Walk.anim.meta").write_text(meta)

            # Write a minimal .controller file referencing the clip
            ctrl_yaml = textwrap.dedent("""\
                %YAML 1.1
                %TAG !u! tag:unity3d.com,2011:
                --- !u!91 &9100000
                AnimatorController:
                  m_ObjectHideFlags: 0
                  m_Name: HumanoidCtrl
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
            ctrl_path = assets / "HumanoidCtrl.controller"
            ctrl_path.write_text(ctrl_yaml)

            from unity.guid_resolver import build_guid_index
            guid_index = build_guid_index(Path(tmpdir))
            result = convert_animations(Path(tmpdir), guid_index=guid_index)

            # Should have generated at least one animation data module
            assert len(result.animation_data_modules) >= 1
            module_name, module_source = result.animation_data_modules[0]
            assert module_name.startswith("AnimationData_")
            assert "HumanoidCtrl" in module_name
            # Module should contain valid JSON inside the Luau source
            assert "JSONDecode" in module_source
            assert "controller" in module_source
            assert "keyframes" in module_source


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

    def test_export_blend_trees_resolved_clip_names(self) -> None:
        """export_controller_json emits blendTrees with clip names resolved from GUID."""
        bt = BlendTree(
            name="Locomotion",
            param="Speed",
            entries=[
                BlendTreeEntry(threshold=0.0, clip_guid="guid-idle"),
                BlendTreeEntry(threshold=1.0, clip_guid="guid-run"),
            ],
        )
        ctrl = AnimatorController(name="Player")
        ctrl.blend_trees["Locomotion"] = bt
        ctrl.states.append(AnimState(
            name="Move", file_id="300", clip_guid="",
            blend_tree_name="Locomotion",
        ))
        data = export_controller_json(ctrl, clip_name_by_guid={
            "guid-idle": "Idle", "guid-run": "Run",
        })
        assert data["blendTrees"] == {
            "Locomotion": {
                "param": "Speed",
                "clips": [
                    {"clip": "Idle", "threshold": 0.0},
                    {"clip": "Run", "threshold": 1.0},
                ],
            },
        }
        move = next(s for s in data["states"] if s["name"] == "Move")
        assert move["blendTree"] == "Locomotion"

    def test_export_blend_trees_absent_when_no_entries(self) -> None:
        """No blendTrees key emitted when the controller has none."""
        ctrl = AnimatorController(name="Empty")
        data = export_controller_json(ctrl)
        assert "blendTrees" not in data

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
        # No animation_data module for a pure-transform-only controller —
        # the runtime JSON path is reserved for humanoid clips.
        assert not any(name.startswith("AnimationData_Spin") for name, _ in result.animation_data_modules)
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
        assert not result.animation_data_modules

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
        """Scene-scoped names: modules get prefixed by the scene stem."""
        from core.unity_types import ParsedScene
        assets = tmp_path / "Assets"
        assets.mkdir()
        # Humanoid clip so we emit JSON.
        anim = assets / "Walk.anim"
        anim.write_text(textwrap.dedent("""\
            %YAML 1.1
            %TAG !u! tag:unity3d.com,2011:
            --- !u!74 &1
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
                path: Hips
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
              m_Name: Walk
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
        module_names = [name for name, _ in result.animation_data_modules]
        assert any(n == "AnimationData_Level1_Locomotion" for n in module_names), module_names


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

    def test_variant_override_of_controller_uses_new_guid(
        self, tmp_path: Path,
    ) -> None:
        """Codex P2 fix: when a prefab variant overrides an Animator's
        m_Controller, the merged template's referenced_animator_controller_guids
        rebuilds from the modified component graph — base controller GUID
        is dropped and the override GUID takes its place.
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
        assert variant.referenced_animator_controller_guids == merged_refs
        # Base controller is no longer referenced.
        assert "c" * 32 not in variant.referenced_animator_controller_guids
