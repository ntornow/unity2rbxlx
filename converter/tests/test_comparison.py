"""
test_comparison.py -- Tests for the comparison system.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestStateDumper:
    def test_dump_unity_state(self, simple_scene_yaml):
        from unity.scene_parser import parse_scene
        from comparison.state_dumper import dump_unity_state

        scene = parse_scene(Path(__file__).parent / "fixtures" / "simple_scene.yaml")
        state = dump_unity_state(scene)
        assert len(state) > 0
        # Should have entries for each node
        assert any("Cube" in key for key in state)
        assert any("MainCamera" in key for key in state)

    def test_roblox_state_luau(self):
        from comparison.state_dumper import dump_roblox_state_luau
        luau = dump_roblox_state_luau()
        assert "GetDescendants" in luau or "workspace" in luau


class TestStateDiff:
    def test_identical_states(self):
        from comparison.state_diff import diff_states
        state = {
            "Cube": {"position": [1, 2, 3], "size": [1, 1, 1]},
            "Sphere": {"position": [4, 5, 6], "size": [2, 2, 2]},
        }
        result = diff_states(state, state)
        assert result.matched_objects == 2
        assert result.mean_position_error == 0.0

    def test_missing_objects(self):
        from comparison.state_diff import diff_states
        unity = {"A": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        roblox = {"B": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        result = diff_states(unity, roblox)
        assert result.matched_objects == 0
        assert "A" in result.unmatched_unity
        assert "B" in result.unmatched_roblox

    def test_position_diff(self):
        from comparison.state_diff import diff_states
        unity = {"Obj": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        roblox = {"Obj": {"position": [1, 0, 0], "size": [1, 1, 1]}}
        result = diff_states(unity, roblox)
        assert result.matched_objects == 1
        assert result.mean_position_error > 0


class TestReport:
    def test_generate_report(self, tmp_path):
        from comparison.state_diff import StateDiffResult
        from comparison.report import generate_report

        diff = StateDiffResult(
            matched_objects=10,
            unmatched_unity=["MissingA"],
            unmatched_roblox=["ExtraB"],
            position_diffs={"Cube": 0.5},
            rotation_diffs={},
            size_diffs={},
            mean_position_error=0.5,
            mean_rotation_error=0.0,
        )
        report_path = generate_report(
            visual_score=0.85,
            state_diff=diff,
            screenshots={},
            heatmap_path=None,
            output_dir=tmp_path,
        )
        assert report_path.exists()
        content = report_path.read_text()
        assert "0.85" in content
        assert "10" in content
