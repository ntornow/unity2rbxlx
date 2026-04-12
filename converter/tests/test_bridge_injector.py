"""
test_bridge_injector.py -- Unit tests for bridge_injector module.

Tests pattern detection, deduplication, and file reading for Luau bridge injection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.bridge_injector import (
    BRIDGE_SPECS,
    BridgeInjectionResult,
    detect_needed_bridges,
    inject_bridges,
)


# ---------------------------------------------------------------------------
# detect_needed_bridges — pattern matching
# ---------------------------------------------------------------------------


class TestDetectNeededBridges:
    """Verify that each bridge spec triggers on its expected patterns."""

    def test_input_getkey(self):
        result = detect_needed_bridges(["Input.GetKeyDown(Enum.KeyCode.W)"])
        assert "Input.luau" in result.needed

    def test_input_getaxis(self):
        result = detect_needed_bridges(["local h = Input.GetAxis(\"Horizontal\")"])
        assert "Input.luau" in result.needed

    def test_input_getswipe(self):
        result = detect_needed_bridges(["Input.GetSwipe()"])
        assert "Input.luau" in result.needed

    def test_time_deltatime(self):
        result = detect_needed_bridges(["local dt = Time.deltaTime"])
        assert "Time.luau" in result.needed

    def test_time_time(self):
        result = detect_needed_bridges(["local t = Time.time"])
        assert "Time.luau" in result.needed

    def test_time_timescale(self):
        result = detect_needed_bridges(["Time.timeScale = 0.5"])
        assert "Time.luau" in result.needed

    def test_time_fixeddeltatime(self):
        result = detect_needed_bridges(["local fdt = Time.fixedDeltaTime"])
        assert "Time.luau" in result.needed

    def test_coroutine_start(self):
        result = detect_needed_bridges(["Coroutine.Start(function()"])
        assert "Coroutine.luau" in result.needed

    def test_coroutine_waitforseconds(self):
        result = detect_needed_bridges(["Coroutine.WaitForSeconds(2)"])
        assert "Coroutine.luau" in result.needed

    def test_coroutine_waitforendofframe(self):
        result = detect_needed_bridges(["Coroutine.WaitForEndOfFrame()"])
        assert "Coroutine.luau" in result.needed

    def test_coroutine_yield(self):
        result = detect_needed_bridges(["Coroutine.Yield()"])
        assert "Coroutine.luau" in result.needed

    def test_physics_raycast(self):
        result = detect_needed_bridges(["local hit = Physics.Raycast(origin, dir)"])
        assert "physics_queries.luau" in result.needed

    def test_physics_checksphere(self):
        result = detect_needed_bridges(["Physics.CheckSphere(pos, 5)"])
        assert "physics_queries.luau" in result.needed

    def test_physics_overlapsphere(self):
        result = detect_needed_bridges(["Physics.OverlapSphere(pos, 10)"])
        assert "physics_queries.luau" in result.needed

    def test_monobehaviour_new(self):
        result = detect_needed_bridges(["local mb = MonoBehaviour.new(script)"])
        assert "MonoBehaviour.luau" in result.needed

    def test_gameobjectutil_instantiate(self):
        result = detect_needed_bridges(["local obj = GameObjectUtil.Instantiate(template)"])
        assert "GameObjectUtil.luau" in result.needed

    def test_gameobjectutil_destroy(self):
        result = detect_needed_bridges(["GameObjectUtil.Destroy(part)"])
        assert "GameObjectUtil.luau" in result.needed

    def test_gameobjectutil_find(self):
        result = detect_needed_bridges(["local p = GameObjectUtil.Find(\"Player\")"])
        assert "GameObjectUtil.luau" in result.needed

    def test_gameobjectutil_setactive(self):
        result = detect_needed_bridges(["GameObjectUtil.SetActive(part, false)"])
        assert "GameObjectUtil.luau" in result.needed

    def test_gameobjectutil_instantiatefromasset(self):
        result = detect_needed_bridges(["GameObjectUtil.InstantiateFromAsset(id)"])
        assert "GameObjectUtil.luau" in result.needed

    def test_statemachine_new(self):
        result = detect_needed_bridges(["local sm = StateMachine.new()"])
        assert "StateMachine.luau" in result.needed

    def test_require_pattern(self):
        result = detect_needed_bridges([
            'local Input = require(game.ReplicatedStorage:WaitForChild("Input"))'
        ])
        assert "Input.luau" in result.needed


# ---------------------------------------------------------------------------
# detect_needed_bridges — deduplication and edge cases
# ---------------------------------------------------------------------------


class TestDetectDeduplication:
    """Verify already-present scripts are not duplicated."""

    def test_existing_script_skipped_by_filename(self):
        result = detect_needed_bridges(
            ["Input.GetKeyDown(Enum.KeyCode.W)"],
            existing_script_names={"Input.luau"},
        )
        assert "Input.luau" not in result.needed
        assert "Input.luau" in result.already_present

    def test_existing_script_skipped_by_name(self):
        """Pipeline passes script names (no extension), not filenames."""
        result = detect_needed_bridges(
            ["Input.GetKeyDown(Enum.KeyCode.W)"],
            existing_script_names={"Input"},
        )
        assert "Input.luau" not in result.needed
        assert "Input.luau" in result.already_present

    def test_no_patterns_found(self):
        result = detect_needed_bridges(["print('hello world')"])
        assert result.needed == []
        assert result.already_present == []

    def test_multiple_bridges_needed(self):
        result = detect_needed_bridges([
            "local dt = Time.deltaTime\nInput.GetKeyDown(Enum.KeyCode.W)"
        ])
        assert "Time.luau" in result.needed
        assert "Input.luau" in result.needed

    def test_multiple_sources_scanned(self):
        result = detect_needed_bridges([
            "local dt = Time.deltaTime",
            "Physics.Raycast(origin, dir)",
        ])
        assert "Time.luau" in result.needed
        assert "physics_queries.luau" in result.needed

    def test_empty_sources(self):
        result = detect_needed_bridges([])
        assert result.needed == []

    def test_result_type(self):
        result = detect_needed_bridges(["Time.deltaTime"])
        assert isinstance(result, BridgeInjectionResult)


# ---------------------------------------------------------------------------
# inject_bridges — file reading
# ---------------------------------------------------------------------------


class TestInjectBridges:
    """Verify inject_bridges reads .luau files from the runtime directory."""

    def test_reads_real_files(self):
        pairs = inject_bridges(["Time.luau"])
        assert len(pairs) == 1
        filename, source = pairs[0]
        assert filename == "Time.luau"
        assert "deltaTime" in source

    def test_missing_file_skipped(self):
        pairs = inject_bridges(["nonexistent_bridge.luau"])
        assert pairs == []

    def test_empty_list(self):
        pairs = inject_bridges([])
        assert pairs == []

    def test_custom_bridge_dir(self, tmp_path):
        (tmp_path / "TestBridge.luau").write_text("return {}", encoding="utf-8")
        pairs = inject_bridges(["TestBridge.luau"], bridge_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0] == ("TestBridge.luau", "return {}")

    def test_multiple_files(self):
        pairs = inject_bridges(["Time.luau", "Input.luau"])
        assert len(pairs) == 2
        filenames = [p[0] for p in pairs]
        assert "Time.luau" in filenames
        assert "Input.luau" in filenames


# ---------------------------------------------------------------------------
# BRIDGE_SPECS consistency
# ---------------------------------------------------------------------------


class TestBridgeSpecsConsistency:
    """Verify BRIDGE_SPECS references files that exist."""

    def test_all_spec_files_exist(self):
        runtime_dir = Path(__file__).parent.parent / "runtime"
        for spec in BRIDGE_SPECS:
            path = runtime_dir / spec.filename
            assert path.exists(), f"Bridge spec references missing file: {spec.filename}"

    def test_all_specs_have_patterns(self):
        for spec in BRIDGE_SPECS:
            assert len(spec.patterns) > 0, f"Bridge spec {spec.filename} has no patterns"

    def test_spec_count(self):
        assert len(BRIDGE_SPECS) == 7
