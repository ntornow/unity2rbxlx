"""test_roblox_dead_modules.py -- TODO #8: generic Roblox-dead module routing.

Covers the generic Roblox-dead detector, the storage routing consumer (both the
topology AND legacy paths), and the prune-closure pass.

These tests DRIVE THE REAL pipeline path: fixtures are built by feeding real C#
+ real post-coherence Luau shapes into the real producers (the detector, the
real ``classify_storage``, the real prune partitioner). NOTHING pre-stamps the
dead verdict or topology_inputs in a way that bypasses computation.

Genericity guardrail: the water cluster is matched BY BEHAVIOR (mapping coverage
+ inert output), never by name. A RENAMED rendering helper (``OceanShimmer``)
with the same body shape is flagged the same way (test
``test_renamed_rendering_helper_is_flagged_dead``).

Fail-before-fix confirmation is documented per behavioral test in its docstring.
"""

from __future__ import annotations

import pytest

from core.roblox_types import RbxScript
from converter.roblox_dead_modules import (
    classify_module_dead,
    compute_prunable_dead,
    extract_require_edges,
    is_input_side_dead,
    measure_input_coverage,
)
from converter.storage_classifier import (
    classify_storage,
    _decide_script_container_from_topology,
    _decide_script_container_legacy,
    REPLICATED_STORAGE,
    SERVER_STORAGE,
)


# ---------------------------------------------------------------------------
# Realistic C# / Luau fixtures (shapes drawn from the SimpleFPS water cluster
# and real AI-transpiled gameplay output).
# ---------------------------------------------------------------------------

# A shader/render helper C#: API surface dominated by unmapped Shader/GL/Render
# APIs (the WaterBase/Displace shape). No gameplay (no GetComponent here).
_RENDER_CSHARP = """
using UnityEngine;
public class WaterBase : MonoBehaviour {
    public Material sharedMaterial;
    void UpdateShader() {
        Shader.EnableKeyword("WATER_REFLECTIVE");
        Shader.DisableKeyword("WATER_SIMPLE");
        sharedMaterial.shader.maximumLOD = 200;
        Camera.main.depthTextureMode |= DepthTextureMode.Depth;
        bool ok = SystemInfo.SupportsRenderTextureFormat(RenderTextureFormat.Depth);
        GL.invertCulling = true;
    }
}
"""

# Inert transpiled body for the render helper (the converter's stub or an AI
# no-op): only boilerplate + a print + empty handler. No genuine Roblox effect.
_INERT_LUAU = """
-- WaterBase: Unity visual/rendering effect (no Roblox equivalent)
local WaterBase = {}
WaterBase.__index = WaterBase
function WaterBase.new(config)
    return setmetatable({ config = config }, WaterBase)
end
function WaterBase:Awake()
end
return WaterBase
"""

# A real gameplay module: uses a couple of unmapped APIs but its transpiled
# body has a GENUINE Roblox effect (Instance.new + .Parent =) -> hard veto.
_GAMEPLAY_CSHARP = """
using UnityEngine;
public class Spawner : MonoBehaviour {
    public GameObject prefab;
    void Spawn() {
        Shader.EnableKeyword("X");        // a couple of unmapped APIs
        GL.invertCulling = false;
        var go = Instantiate(prefab);     // real gameplay
        go.GetComponent<Rigidbody>().AddForce(Vector3.up);
    }
}
"""

_GAMEPLAY_LUAU = """
local part = Instance.new("Part")
part.Parent = workspace
part.Anchored = false
"""


# ---------------------------------------------------------------------------
# (a) Generic detector flags the water cluster dead (behavior-based)
# ---------------------------------------------------------------------------


def test_render_helper_flagged_dead_by_behavior():
    """The generic detector flags a render helper dead from its
    mapping-coverage prior + inert output -- NO class-name list involved.

    Fail-before-fix: this module/function did not exist before this change
    (``classify_module_dead`` is new). The pre-fix dead detection was the
    hardcoded name list in ``_is_visual_only_script``; this test exercises the
    generic replacement on a behavioral fixture.
    """
    verdict = classify_module_dead(
        "WaterBase", csharp_source=_RENDER_CSHARP, luau_source=_INERT_LUAU,
    )
    assert verdict.is_dead, verdict.reason
    assert verdict.output_inert
    assert not verdict.vetoed
    assert verdict.input_coverage.dead_leaning


# ---------------------------------------------------------------------------
# (b) Genericity + hard veto
# ---------------------------------------------------------------------------


# A renamed rendering helper whose unmapped APIs are NOT in the old
# ``_is_visual_only_script`` shader list (GL / depthTextureMode / cullingMask /
# SystemInfo) and that has only ONE old-list hit (``RenderTexture``), so the old
# ``shader_count >= 2`` heuristic AND the name list BOTH missed it. The generic
# coverage detector flags it on behavior alone.
_RENAMED_RENDER_CSHARP = """
using UnityEngine;
public class OceanShimmer : MonoBehaviour {
    void Refresh() {
        GL.invertCulling = true;
        Camera.main.depthTextureMode |= DepthTextureMode.Depth;
        Camera.main.cullingMask = 0;
        bool ok = SystemInfo.SupportsRenderTextureFormat(RenderTextureFormat.ARGBHalf);
        GL.modelview = Matrix4x4.identity;
    }
}
"""


def test_renamed_rendering_helper_is_flagged_dead():
    """A renamed rendering helper (``OceanShimmer``) with no name-list match and
    only ONE old-shader-list hit is flagged dead -- proves the detector is
    BEHAVIOR-driven, not name-driven.

    Fail-before-fix CONFIRMED below: the OLD ``_is_visual_only_script`` (a) name
    list does not contain ``oceanshimmer`` and (b) ``shader_count >= 2``
    heuristic counts only 1 hit (``RenderTexture``) in this body -> OLD returned
    NOT dead. The new generic detector flags it. The in-test reimplementation of
    the OLD heuristic witnesses the pre-fix miss.
    """
    csharp = _RENAMED_RENDER_CSHARP
    luau = _INERT_LUAU.replace("WaterBase", "OceanShimmer")

    # Fail-before-fix witness: the OLD heuristic returns NOT dead.
    old_shader_list = [
        "Shader.", "Material.", "Renderer.", "renderer.material",
        "OnRenderImage", "OnWillRenderObject", "RenderTexture", "Graphics.Blit",
    ]
    old_name_list = {
        "planarreflection", "specularlighting", "waterbase", "watertile",
        "waterbasic", "gerstnerdisplace", "displace", "meshcontainer",
        "planetexture",
    }
    old_dead = (
        "oceanshimmer" in old_name_list
        or sum(1 for s in old_shader_list if s in csharp) >= 2
    )
    assert not old_dead, (
        "fail-before-fix witness: the OLD name+shader_count heuristic misses "
        "this renamed helper"
    )

    verdict = classify_module_dead(
        "OceanShimmer", csharp_source=csharp, luau_source=luau,
    )
    assert verdict.is_dead, verdict.reason
    # And the transpile-time input gate also catches the shader-only shape.
    assert is_input_side_dead(csharp)


def test_gameplay_module_with_real_effect_is_not_dead_hard_veto():
    """A real gameplay module that uses a couple unmapped APIs but writes a real
    Instance is NOT flagged dead -- the HARD VETO wins regardless of input
    fraction.

    Fail-before-fix: N/A as a regression (the veto is new behavior), but the
    test pins the LOCKED DECISION: a single genuine Roblox effect vetoes
    deadness. Without the veto, a low-coverage module with an inert-looking
    metric could be flagged.
    """
    verdict = classify_module_dead(
        "Spawner", csharp_source=_GAMEPLAY_CSHARP, luau_source=_GAMEPLAY_LUAU,
    )
    assert not verdict.is_dead
    assert verdict.vetoed


def test_gameplay_csharp_vetoes_input_side_gate():
    """The transpile-time input gate never stubs a gameplay C# body (GetComponent
    / Instantiate), even if mapping coverage is low."""
    assert not is_input_side_dead(_GAMEPLAY_CSHARP)


# ---------------------------------------------------------------------------
# (c) Routing: a dead ModuleScript with only server-domain callers does NOT
#     land in ServerStorage (both topology AND legacy paths).
# ---------------------------------------------------------------------------


def _topology_inputs(*, module_sid: str, caller_sid: str):
    from converter.scene_runtime_topology.module_domain import TopologyInputs

    inputs: TopologyInputs = {
        "domains": {caller_sid: "server"},
        "reachability_requirements": {},
        "lifecycle_roles": {},
        "script_id_by_name": {
            "DeadHelper": module_sid, "ServerLeaf": caller_sid,
        },
        "caller_graph": {module_sid: [caller_sid]},
        "transpile_ran": True,
    }
    return inputs


def test_topology_dead_module_server_callers_routes_to_replicated_not_server():
    """Topology path: a dead ModuleScript required ONLY by server-domain callers
    routes to ReplicatedStorage when in ``dead_modules`` -- not ServerStorage.

    Fail-before-fix CONFIRMED: with ``dead_modules`` empty (pre-fix behavior),
    the same inputs route the module to ServerStorage (asserted below). The
    dead reroute is what flips it to ReplicatedStorage.
    """
    inputs = _topology_inputs(module_sid="g-dead", caller_sid="g-srv")
    dead = RbxScript(name="DeadHelper", source="local M={}\nreturn M",
                     script_type="ModuleScript")

    # Pre-fix behavior witness: no dead set -> ServerStorage.
    cont_nodead, _ = _decide_script_container_from_topology(
        dead, sid="g-dead", topology_inputs=inputs, dead_modules=frozenset(),
    )
    assert cont_nodead == SERVER_STORAGE, (
        "fail-before-fix witness: without the dead set, server-only callers "
        "pull the module into ServerStorage (the symptom)"
    )

    # With the dead set -> ReplicatedStorage.
    cont_dead, reason = _decide_script_container_from_topology(
        dead, sid="g-dead", topology_inputs=inputs,
        dead_modules=frozenset({"DeadHelper"}),
    )
    assert cont_dead == REPLICATED_STORAGE, reason
    assert "Roblox-dead" in reason


def test_legacy_dead_module_server_callers_routes_to_replicated_not_server():
    """Legacy path: the cached SimpleFPS symptom uses THIS path's
    ``...server-side callers`` reason text, so the reroute must fire here too.

    Fail-before-fix CONFIRMED: with ``dead_modules`` empty, the legacy decider
    returns ServerStorage (asserted). With the dead set it returns
    ReplicatedStorage.
    """
    server_leaf = RbxScript(
        name="ServerLeaf",
        source='local U = require(game:GetService("ServerStorage")'
               ':FindFirstChild("DeadHelper"))',
        script_type="Script",
    )
    dead = RbxScript(name="DeadHelper", source="local M={}\nreturn M",
                     script_type="ModuleScript")
    from converter.storage_classifier import _build_call_graph
    call_graph = _build_call_graph([server_leaf, dead], None)
    script_by_name = {s.name: s for s in [server_leaf, dead]}

    cont_nodead, _ = _decide_script_container_legacy(
        dead, call_graph=call_graph, character_set=set(),
        script_by_name=script_by_name, server_touchers={"ServerLeaf"},
        dead_modules=frozenset(),
    )
    assert cont_nodead == SERVER_STORAGE, (
        "fail-before-fix witness: legacy path routes server-only-required "
        "dead module to ServerStorage (the cached symptom)"
    )

    cont_dead, reason = _decide_script_container_legacy(
        dead, call_graph=call_graph, character_set=set(),
        script_by_name=script_by_name, server_touchers={"ServerLeaf"},
        dead_modules=frozenset({"DeadHelper"}),
    )
    assert cont_dead == REPLICATED_STORAGE, reason
    assert "Roblox-dead" in reason


def test_classify_storage_end_to_end_dead_reroute():
    """End-to-end through ``classify_storage`` (no topology_inputs -> legacy):
    a dead module required by a server Script lands in ReplicatedStorage, not
    ServerStorage, when passed via ``dead_modules``."""
    server_leaf = RbxScript(
        name="ServerLeaf",
        source='local DSS = game:GetService("DataStoreService")\n'
               'local U = require(game:GetService("ServerStorage")'
               ':FindFirstChild("DeadHelper"))',
        script_type="Script",
    )
    dead = RbxScript(name="DeadHelper", source="local M={}\nreturn M",
                     script_type="ModuleScript")

    plan_nodead = classify_storage([server_leaf, dead])
    assert dead.parent_path == SERVER_STORAGE  # pre-fix witness
    assert "DeadHelper" in plan_nodead.server_modules

    dead.parent_path = None
    plan = classify_storage(
        [server_leaf, dead], dead_modules=frozenset({"DeadHelper"}),
    )
    assert dead.parent_path == REPLICATED_STORAGE
    assert "DeadHelper" in plan.shared_modules
    assert "DeadHelper" not in plan.server_modules


# ---------------------------------------------------------------------------
# (d) Prune: a fully-dead require-closure is dropped.
# ---------------------------------------------------------------------------


def _edges(scripts: list[RbxScript]) -> dict[str, set[str]]:
    known = frozenset(s.name for s in scripts)
    return {s.name: extract_require_edges(s.source, known) for s in scripts}


def test_fully_dead_closure_is_prunable():
    """A self-contained dead cluster (dead modules requiring only each other)
    is entirely safe to prune.

    Fail-before-fix: ``compute_prunable_dead`` is new; the prior pipeline had no
    prune-closure pass. This pins the safe-subset rule.
    """
    # PlanarReflection requires WaterBase; both dead; nobody live requires them.
    planar = RbxScript(
        name="PlanarReflection",
        source='local WaterBase = require(game:GetService("ReplicatedStorage")'
               ':FindFirstChild("WaterBase", true) or '
               'game:GetService("ServerStorage"):FindFirstChild("WaterBase", true))\n'
               "local M={}\nreturn M",
        script_type="ModuleScript",
    )
    waterbase = RbxScript(name="WaterBase", source="local M={}\nreturn M",
                          script_type="ModuleScript")
    edges = _edges([planar, waterbase])
    assert edges["PlanarReflection"] == {"WaterBase"}

    result = compute_prunable_dead(
        frozenset({"PlanarReflection", "WaterBase"}), edges,
    )
    assert result.prunable == {"PlanarReflection", "WaterBase"}
    assert result.keep_inert == set()


# ---------------------------------------------------------------------------
# (e) Prune safety: a dead module with a LIVE requirer is NOT dropped.
# ---------------------------------------------------------------------------


def test_dead_module_with_live_requirer_is_kept_inert():
    """A dead module required by a LIVE (non-dead) module must NOT be pruned --
    dropping it would leave the live module's ``require()`` resolving to nil
    (``require(nil)`` crash, GF8).

    Fail-before-fix: ``compute_prunable_dead`` is new; this pins the GF8 safety
    invariant -- the dead-but-live-required module stays as inert/reroute.
    """
    live = RbxScript(
        name="LiveGameLogic",
        source='local Dead = require(game:GetService("ReplicatedStorage")'
               ':FindFirstChild("DeadHelper", true) or '
               'game:GetService("ServerStorage"):FindFirstChild("DeadHelper", true))\n'
               "local part = Instance.new('Part')\npart.Parent = workspace",
        script_type="ModuleScript",
    )
    dead = RbxScript(name="DeadHelper", source="local M={}\nreturn M",
                     script_type="ModuleScript")
    edges = _edges([live, dead])
    assert edges["LiveGameLogic"] == {"DeadHelper"}

    # Only DeadHelper is dead; LiveGameLogic is live.
    result = compute_prunable_dead(frozenset({"DeadHelper"}), edges)
    assert result.prunable == set(), (
        "a dead module with a live requirer must never be pruned"
    )
    assert result.keep_inert == {"DeadHelper"}


# ---------------------------------------------------------------------------
# (f) No regression: a non-dead module set runs through with zero dead verdicts.
# ---------------------------------------------------------------------------


def test_no_false_dead_on_normal_module_set():
    """A normal gameplay module set yields ZERO dead verdicts.

    Fail-before-fix: N/A (no-regression guard). Ensures the generic detector
    does not over-flag ordinary modules whose bodies have real effects.
    """
    door_csharp = (
        "using UnityEngine;\n"
        "public class Door : MonoBehaviour {\n"
        "  void OnTriggerEnter(Collider c) {\n"
        "    transform.position = transform.position + Vector3.up;\n"
        "    GetComponent<Rigidbody>().isKinematic = true;\n"
        "  }\n}\n"
    )
    door_luau = (
        "local door = script.Parent\n"
        "door.CFrame = door.CFrame * CFrame.new(0, 5, 0)\n"
        "door.Anchored = true\n"
    )
    v = classify_module_dead(
        "Door", csharp_source=door_csharp, luau_source=door_luau,
    )
    assert not v.is_dead, v.reason


def test_trivial_empty_module_is_not_dead():
    """A content-free module (no measurable API surface, trivial valid body) is
    NOT flagged dead -- the input prior must AGREE (measured + dead-leaning),
    abstention alone is never a dead verdict.

    Fail-before-fix witness for the abstain-is-not-dead rule: this exact shape
    (``// stub`` C# + ``local M={} return M`` Luau) is what the
    materialize-and-classify end-to-end fixture uses; an earlier draft that let
    abstention license deadness pruned it. This locks the corrected rule.
    """
    v = classify_module_dead(
        "UserModule", csharp_source="// stub", luau_source="local M={}\nreturn M\n",
    )
    assert not v.is_dead, v.reason
    assert not v.input_coverage.measured


# ---------------------------------------------------------------------------
# Pipeline end-to-end: drive the REAL materialize_and_classify dead-module
# analysis + prune + reroute. No pre-stamped verdicts -- the producers compute.
# ---------------------------------------------------------------------------


def _make_pipeline(tmp_path):
    from converter.pipeline import Pipeline

    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


def test_pipeline_prunes_orphan_dead_cluster_end_to_end(tmp_path):
    """Drive the real ``materialize_and_classify``: an orphan dead cluster
    (no requirers, ``stub`` strategy) is analyzed dead and PRUNED from
    ``rbx_place.scripts``; a live gameplay module survives.

    Fail-before-fix: the prune pass (``_subphase_prune_dead_module_closures``)
    + analysis pass are new; before this change ``rbx_place.scripts`` kept the
    dead modules. Driven through the real phase (no pre-stamped dead set).
    """
    from converter.code_transpiler import TranspilationResult, TranspiledScript
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
        RbxScript(
            name="LiveLogic",
            source="local part = Instance.new('Part')\npart.Parent = workspace\n",
            script_type="Script",
        ),
    ]
    pipeline.state.transpilation_result = TranspilationResult(
        scripts=[
            TranspiledScript(
                source_path="Assets/WaterBase.cs",
                output_filename="WaterBase.luau",
                csharp_source=_RENDER_CSHARP,
                luau_source=_INERT_LUAU,
                strategy="stub",
                confidence=1.0,
                script_type="ModuleScript",
            ),
            TranspiledScript(
                source_path="Assets/LiveLogic.cs",
                output_filename="LiveLogic.luau",
                csharp_source=_GAMEPLAY_CSHARP,
                luau_source="local part = Instance.new('Part')\npart.Parent = workspace\n",
                strategy="ai",
                confidence=1.0,
                script_type="Script",
            ),
        ],
        total_transpiled=2,
        total_ai=1,
    )

    # Run only the analysis + prune subphases (avoid the full classify, which
    # needs more scene wiring); these are the units under test.
    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset({"WaterBase"})

    pipeline._subphase_prune_dead_module_closures()
    names = {s.name for s in pipeline.state.rbx_place.scripts}
    assert "WaterBase" not in names, "orphan dead cluster must be pruned"
    assert "LiveLogic" in names


def test_pipeline_keeps_dead_module_with_live_requirer_end_to_end(tmp_path):
    """Drive the real phase: a dead module REQUIRED by a live module is NOT
    pruned (GF8) -- it stays in ``rbx_place.scripts`` so the surviving
    ``require()`` resolves.

    Fail-before-fix: the prune-safety partition is new; this pins that a live
    requirer blocks the prune (no ``require(nil)``).
    """
    from converter.code_transpiler import TranspilationResult, TranspiledScript
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    live_src = (
        'local Dead = require(game:GetService("ReplicatedStorage")'
        ':FindFirstChild("WaterBase", true) or '
        'game:GetService("ServerStorage"):FindFirstChild("WaterBase", true))\n'
        "local part = Instance.new('Part')\npart.Parent = workspace\n"
    )
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
        RbxScript(name="LiveLogic", source=live_src, script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = TranspilationResult(
        scripts=[
            TranspiledScript(
                source_path="Assets/WaterBase.cs",
                output_filename="WaterBase.luau",
                csharp_source=_RENDER_CSHARP, luau_source=_INERT_LUAU,
                strategy="stub", confidence=1.0, script_type="ModuleScript",
            ),
            TranspiledScript(
                source_path="Assets/LiveLogic.cs",
                output_filename="LiveLogic.luau",
                csharp_source=_GAMEPLAY_CSHARP, luau_source=live_src,
                strategy="ai", confidence=1.0, script_type="ModuleScript",
            ),
        ],
        total_transpiled=2, total_ai=1,
    )

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset({"WaterBase"})

    pipeline._subphase_prune_dead_module_closures()
    names = {s.name for s in pipeline.state.rbx_place.scripts}
    assert "WaterBase" in names, (
        "a dead module with a live requirer must stay emitted (no require(nil))"
    )
    assert "LiveLogic" in names


def test_pipeline_resume_without_transpilation_result_abstains(tmp_path):
    """On a no-transpile resume (``transpilation_result is None``) the pass
    abstains entirely -- no dead verdicts, no prune. Preserves the storage plan
    computed on the run that transpiled."""
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = None

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset()
    pipeline._subphase_prune_dead_module_closures()
    assert {s.name for s in pipeline.state.rbx_place.scripts} == {"WaterBase"}
