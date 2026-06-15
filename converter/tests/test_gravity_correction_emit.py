"""Phase-1 (relation #8) emit tests: the emit-gate predicate, the baked
standalone ``SceneGravityCorrection`` server script, the ``write_output``
emit subphase, and the producer→consumer stash hop.

Covers AC1-8 / AC5b / AC8b / AC8c / AC8e / AC10 / AC10b / AC11 / AC12 / AC16b.
Force-shape tokens are STRUCTURAL source assertions (FIX A); emit-gate and
zero-gravity survival are pure-Python pytest. Numeric net-accel ⇒ Studio S2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _config
from converter.autogen import generate_gravity_correction_server_script
from converter.pipeline import Pipeline
from core.conversion_context import ConversionContext
from core.roblox_types import RbxPart, RbxPlace, RbxScript
from utils import luau_analyze


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _dynamic_part(name: str = "Crate", **attrs: object) -> RbxPart:
    base = {"_UnityMass": 2.0}
    base.update(attrs)
    return RbxPart(name=name, class_name="Part", anchored=False, attributes=base)


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.ctx.scene_runtime_mode = "generic"
    pipeline.ctx.scene_runtime = {"gravityDesiredBaseStuds": 35.03}
    return pipeline


def _write_dynamics_manager(unity_project: Path, *, y: float) -> None:
    """Write a real-shaped ProjectSettings/DynamicsManager.asset so
    ``plan_scene_runtime`` parses an actual gravity scalar (not the default)."""
    settings = unity_project / "ProjectSettings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "DynamicsManager.asset").write_text(
        "%YAML 1.1\n"
        "%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!55 &1\n"
        "PhysicsManager:\n"
        f"  m_Gravity: {{x: 0, y: {y}, z: 0}}\n",
        encoding="utf-8",
    )


def _make_pipeline_with_real_gravity(tmp_path: Path, *, y: float) -> Pipeline:
    """A pipeline whose Unity project carries a real DynamicsManager.asset and
    whose stash is NOT pre-seeded -- so ``plan_scene_runtime`` is the producer."""
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    _write_dynamics_manager(unity_project, y=y)
    output = tmp_path / "out"
    output.mkdir()
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.ctx.scene_runtime_mode = "generic"
    return pipeline


# --------------------------------------------------------------------------
# AC11 / AC8e -- emit-gate predicate
# --------------------------------------------------------------------------
class TestEmitGatePredicate:
    def test_fires_on_scene_dynamic_unitymass(self) -> None:
        parts = [_dynamic_part()]
        assert Pipeline._part_tree_has_dynamic_unitymass(parts) is True

    def test_fires_on_nested_dynamic_unitymass_recursively(self) -> None:
        inner = _dynamic_part("Inner")
        container = RbxPart(name="Container", class_name="Model", children=[inner])
        assert Pipeline._part_tree_has_dynamic_unitymass([container]) is True

    def test_no_fire_when_no_dynamic_unitymass(self) -> None:
        static = RbxPart(name="Floor", class_name="Part")
        assert Pipeline._part_tree_has_dynamic_unitymass([static]) is False

    def test_no_fire_on_rigidbody2d_only(self) -> None:
        """AC8e: a 2D-only game (every dynamic part carries _Rigidbody2D) does
        NOT count toward the gate -- Physics2D is OOS."""
        twod = _dynamic_part("Coin2D", _Rigidbody2D=True)
        assert Pipeline._part_tree_has_dynamic_unitymass([twod]) is False

    def test_no_fire_on_mesh_wrapped_2d_body(self) -> None:
        """AC8e: a mesh-wrapped 2D body -- the inner *_Mesh carrier holds BOTH
        _UnityMass and _Rigidbody2D (co-located via the move-list) -- is skipped."""
        inner = _dynamic_part("Coin_Mesh", _Rigidbody2D=True)
        outer = RbxPart(name="Coin", class_name="Model", children=[inner])
        assert Pipeline._part_tree_has_dynamic_unitymass([outer]) is False

    def test_fires_when_3d_present_alongside_2d(self) -> None:
        twod = _dynamic_part("Coin2D", _Rigidbody2D=True)
        threed = _dynamic_part("Crate3D")
        assert Pipeline._part_tree_has_dynamic_unitymass([twod, threed]) is True

    def test_bool_unitymass_is_not_numeric(self) -> None:
        """A bool is an int subclass in Python; it must NOT count as a numeric
        _UnityMass mass."""
        weird = RbxPart(name="X", anchored=False, attributes={"_UnityMass": True})
        assert Pipeline._part_tree_has_dynamic_unitymass([weird]) is False


# --------------------------------------------------------------------------
# AC1-8 / AC10 -- baked server script source
# --------------------------------------------------------------------------
class TestBakedServerScript:
    def test_baked_constant_uses_repr_float(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        assert "local DESIRED_G_STUDS_BASE = " + repr(35.03) in src

    def test_abs_scalar_default_target_baked(self) -> None:
        g = abs(-9.81) * _config.STUDS_PER_METER
        src = generate_gravity_correction_server_script(g)
        assert "local DESIRED_G_STUDS_BASE = " + repr(g) in src

    def test_zero_gravity_constant_survives(self) -> None:
        """AC10b at the generator: a 0.0 base bakes 0.0 (full-cancel), NOT a
        truthy default."""
        src = generate_gravity_correction_server_script(0.0)
        assert "local DESIRED_G_STUDS_BASE = " + repr(0.0) in src

    def test_tag_literal_and_helper_embedded(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        assert 'local TAG = "_ScaleGravityCorrected"' in src
        assert "local function correctDynamicAssembly(carrier, desiredBaseStuds)" in src

    def test_boot_sweep_class_agnostic_with_2d_exclusion(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        assert "for _, d in workspace:GetDescendants() do" in src
        assert (
            'd:GetAttribute("_UnityMass") ~= nil and '
            'd:GetAttribute("_Rigidbody2D") == nil' in src
        )

    def test_descendant_added_hook_deferred_with_2d_exclusion(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        assert "workspace.DescendantAdded:Connect(function(d)" in src
        assert "task.defer(function() correctDynamicAssembly(d, DESIRED_G_STUDS_BASE) end)" in src

    @pytest.mark.skipif(
        luau_analyze.luau_analyze_path() is None,
        reason="needs luau-analyze for the syntax smoke test",
    )
    def test_emitted_source_is_syntactically_valid(self) -> None:
        """AC10: the emitted Luau LOADS (no SyntaxError). Roblox-API TypeErrors
        are filtered out by syntax_errors_for_source."""
        src = generate_gravity_correction_server_script(35.03)
        errors = luau_analyze.syntax_errors_for_source(src)
        assert errors == [], errors


# --------------------------------------------------------------------------
# AC8c / AC8e -- the _UnityMass/_Rigidbody2D guard is present on BOTH scan
# surfaces (boot sweep AND DescendantAdded) and is class-agnostic (NOT gated
# on IsA("BasePart")).
# --------------------------------------------------------------------------
class TestBothScanSurfacesGuarded:
    _GUARD = (
        'd:GetAttribute("_UnityMass") ~= nil and '
        'd:GetAttribute("_Rigidbody2D") == nil'
    )

    def _boot_and_added(self, src: str) -> tuple[str, str]:
        """Split the emitted source into (boot-sweep region, DescendantAdded
        region) so each surface is asserted independently. A guard dropped from
        EITHER surface must fail the assertions below, not be masked by the other
        surface still carrying it."""
        boot_start = src.index("for _, d in workspace:GetDescendants()")
        added_start = src.index("workspace.DescendantAdded:Connect")
        assert boot_start < added_start, "boot sweep must precede the spawn hook"
        return src[boot_start:added_start], src[added_start:]

    def test_boot_sweep_carries_the_guard(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        boot, _ = self._boot_and_added(src)
        assert self._GUARD in boot, "boot sweep dropped the _UnityMass/_Rigidbody2D guard"

    def test_descendant_added_carries_the_guard(self) -> None:
        src = generate_gravity_correction_server_script(35.03)
        _, added = self._boot_and_added(src)
        assert self._GUARD in added, (
            "DescendantAdded path dropped the _UnityMass/_Rigidbody2D guard"
        )

    def test_scan_is_not_gated_by_isa_basepart(self) -> None:
        """The scan selects on the _UnityMass attribute regardless of class
        (a Model carrier S3/S6 must be admitted, then skipped via skip-if-anchored
        inside the helper). A regression to IsA("BasePart") on the scan would
        silently drop Model carriers, so neither surface may gate the descendant
        ``d`` on IsA("BasePart") before the attribute test."""
        src = generate_gravity_correction_server_script(35.03)
        boot, added = self._boot_and_added(src)
        for region, label in ((boot, "boot sweep"), (added, "DescendantAdded")):
            assert 'd:IsA("BasePart")' not in region, (
                f"{label} reverted to a BasePart-only scan (class-agnostic required)"
            )


# --------------------------------------------------------------------------
# AC11 / AC12 -- the write_output emit subphase
# --------------------------------------------------------------------------
class TestEmitSubphase:
    def test_emits_when_scene_has_dynamic(self, tmp_path: Path) -> None:
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        names = [s.name for s in p.state.rbx_place.scripts]
        assert names.count("SceneGravityCorrection") == 1

    def test_emits_when_only_prefab_template_has_dynamic(self, tmp_path: Path) -> None:
        """AC11: prefab-clone-only game -- dynamic part ONLY in
        replicated_templates, none in workspace_parts."""
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [RbxPart(name="Floor")]
        p.state.rbx_place.replicated_templates = [_dynamic_part("CrateTemplate")]
        p._subphase_inject_gravity_correction()
        assert any(
            s.name == "SceneGravityCorrection" for s in p.state.rbx_place.scripts
        )

    def test_no_emit_when_no_dynamic_anywhere(self, tmp_path: Path) -> None:
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [RbxPart(name="Floor")]
        p._subphase_inject_gravity_correction()
        assert not any(
            s.name == "SceneGravityCorrection" for s in p.state.rbx_place.scripts
        )

    def test_no_emit_when_not_generic(self, tmp_path: Path) -> None:
        p = _make_pipeline(tmp_path)
        p.ctx.scene_runtime_mode = "legacy"
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        assert not any(
            s.name == "SceneGravityCorrection" for s in p.state.rbx_place.scripts
        )

    def test_emitted_script_routes_to_server_script_service(self, tmp_path: Path) -> None:
        """AC12: parent_path == ServerScriptService, script_type == Script."""
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        s = next(
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        )
        assert s.parent_path == "ServerScriptService"
        assert s.script_type == "Script"

    def test_idempotent_on_rerun(self, tmp_path: Path) -> None:
        """AC12: re-running write_output (--phase resume) does not duplicate."""
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        p._subphase_inject_gravity_correction()
        names = [s.name for s in p.state.rbx_place.scripts]
        assert names.count("SceneGravityCorrection") == 1

    def test_does_not_clobber_user_named_script(self, tmp_path: Path) -> None:
        p = _make_pipeline(tmp_path)
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        user = RbxScript(
            name="SceneGravityCorrection",
            source="-- my own script",
            script_type="Script",
        )
        p.state.rbx_place.scripts.append(user)
        p._subphase_inject_gravity_correction()
        sgc = [
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        ]
        assert len(sgc) == 1
        assert sgc[0].source == "-- my own script"


# --------------------------------------------------------------------------
# AC10b -- zero-gravity survives into the emitted constant (Python falsy guard)
# --------------------------------------------------------------------------
class TestZeroGravitySurvives:
    def test_stashed_zero_bakes_zero_not_default(self, tmp_path: Path) -> None:
        p = _make_pipeline(tmp_path)
        p.ctx.scene_runtime = {"gravityDesiredBaseStuds": 0.0}
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        s = next(
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        )
        assert "local DESIRED_G_STUDS_BASE = " + repr(0.0) in s.source
        default = _config.STUDS_PER_METER * 9.81
        assert repr(default) not in s.source

    def test_missing_stash_falls_back_to_default(self, tmp_path: Path) -> None:
        """The is-None default still covers a legacy path that reached
        write_output without plan_scene_runtime populating the key."""
        p = _make_pipeline(tmp_path)
        p.ctx.scene_runtime = {}
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        s = next(
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        )
        default = _config.STUDS_PER_METER * 9.81
        assert "local DESIRED_G_STUDS_BASE = " + repr(default) in s.source


# --------------------------------------------------------------------------
# AC16b -- producer→consumer stash rehydration on the SAME ctx
# --------------------------------------------------------------------------
class TestStashRehydration:
    def test_server_script_bakes_the_stashed_value(self, tmp_path: Path) -> None:
        """The value the server script bakes equals the value the stash carries
        (the 1.1 producer → 1.2 consumer hop), not a re-parse."""
        p = _make_pipeline(tmp_path)
        stashed = 12.5 * _config.STUDS_PER_METER
        p.ctx.scene_runtime = {"gravityDesiredBaseStuds": stashed}
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        s = next(
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        )
        assert "local DESIRED_G_STUDS_BASE = " + repr(stashed) in s.source

    def test_real_producer_to_consumer_hop_same_pipeline(self, tmp_path: Path) -> None:
        """AC16b real hop: drive the REAL producer (``plan_scene_runtime``) on the
        SAME Pipeline that the consumer (``_subphase_inject_gravity_correction``)
        runs on -- no manually-seeded stash. The server script must bake the
        scalar parsed from the project's DynamicsManager.asset, proving the
        producer→consumer wiring (not a stubbed dict read)."""
        p = _make_pipeline_with_real_gravity(tmp_path, y=-12.5)
        assert "gravityDesiredBaseStuds" not in p.ctx.scene_runtime, (
            "guard: the stash must be empty BEFORE the producer runs"
        )
        # Producer: parses DynamicsManager.asset and stashes the scalar.
        p.plan_scene_runtime()
        expected = 12.5 * _config.STUDS_PER_METER
        assert p.ctx.scene_runtime.get("gravityDesiredBaseStuds") == expected
        # Consumer: reads the SAME ctx stash and bakes the literal.
        p.state.rbx_place.workspace_parts = [_dynamic_part()]
        p._subphase_inject_gravity_correction()
        s = next(
            s for s in p.state.rbx_place.scripts if s.name == "SceneGravityCorrection"
        )
        assert "local DESIRED_G_STUDS_BASE = " + repr(expected) in s.source

    def test_stashed_scalar_survives_context_save_load_resume(
        self, tmp_path: Path
    ) -> None:
        """AC16b resume: the stashed scalar survives a ConversionContext
        save→load round-trip (the real serialization API), so a ``--phase``
        resume rehydrates it and the consumer bakes the SAME value -- it is not
        re-parsed and not lost across the serialize/deserialize boundary."""
        p = _make_pipeline_with_real_gravity(tmp_path, y=-12.5)
        p.plan_scene_runtime()
        expected = 12.5 * _config.STUDS_PER_METER

        # Serialize → deserialize via the real ConversionContext JSON API.
        ctx_path = tmp_path / "conversion_context.json"
        p.ctx.save(ctx_path)
        rehydrated = ConversionContext.load(ctx_path)
        assert rehydrated.scene_runtime.get("gravityDesiredBaseStuds") == expected

        # A resumed pipeline consuming the rehydrated ctx bakes the SAME value.
        resumed = _make_pipeline(tmp_path / "resume")
        resumed.ctx.scene_runtime = rehydrated.scene_runtime
        resumed.state.rbx_place.workspace_parts = [_dynamic_part()]
        resumed._subphase_inject_gravity_correction()
        s = next(
            s
            for s in resumed.state.rbx_place.scripts
            if s.name == "SceneGravityCorrection"
        )
        assert "local DESIRED_G_STUDS_BASE = " + repr(expected) in s.source
