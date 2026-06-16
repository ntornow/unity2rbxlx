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
    csharp_source_has_rendering_api,
    extract_require_edges,
    has_genuine_roblox_effect,
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
    """On a no-transpile resume (``transpilation_result is None``) with NO
    persisted dead set, the pass abstains entirely -- no dead verdicts, no
    prune. Preserves the storage plan computed on the run that transpiled."""
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


# ---------------------------------------------------------------------------
# P1-a: the transpile-time stub gate requires a POSITIVE rendering-API signal.
# A portable menu / save / scene controller (low coverage, NO rendering API) is
# NOT stubbed; a true rendering helper (rendering API present) IS.
# ---------------------------------------------------------------------------


# A portable menu / save / scene controller: low mapping coverage, gameplay-
# adjacent persistence + scene-management behavior, but ZERO rendering APIs.
_MENU_CSHARP = """
using UnityEngine;
using UnityEngine.SceneManagement;
public class MenuController : MonoBehaviour {
    void Start() {
        Cursor.lockState = CursorLockMode.None;
    }
    void OnPlay() {
        PlayerPrefs.SetInt("started", 1);
        SceneManager.LoadScene("Game");
    }
    void OnQuit() {
        Application.Quit();
    }
}
"""

# A single-rendering-API helper: ONE rendering token (Shader.) plus other
# render-target APIs, no gameplay -> still stubbed.
_SINGLE_RENDER_CSHARP = """
using UnityEngine;
public class Glow : MonoBehaviour {
    void Refresh() {
        Shader.EnableKeyword("GLOW");
        sharedMaterial.shader.maximumLOD = 100;
        Camera.main.depthTextureMode |= DepthTextureMode.Depth;
        bool ok = SystemInfo.SupportsRenderTextureFormat(RenderTextureFormat.Depth);
    }
}
"""


def test_menu_controller_not_stubbed_at_transpile_time():
    """P1-a: a portable menu/save/scene controller (low coverage, ZERO rendering
    APIs) must NOT be stubbed by the destructive transpile-time gate.

    Fail-before-fix CONFIRMED: before requiring a positive rendering signal,
    ``is_input_side_dead`` returned True for this body (no gameplay veto + low
    coverage was sufficient), silently dropping portable behavior. The witness:
    the body has NO rendering-API signal yet coverage is dead-leaning, so the
    pre-fix (gameplay-veto + coverage only) gate would have stubbed it.
    """
    # Witness: coverage IS dead-leaning and there is NO gameplay veto, so the
    # OLD gate (which lacked the rendering-signal requirement) would stub it.
    assert measure_input_coverage(_MENU_CSHARP).dead_leaning
    assert not csharp_source_has_rendering_api(_MENU_CSHARP)
    # New gate: NOT dead (no positive rendering signal).
    assert not is_input_side_dead(_MENU_CSHARP)


def test_single_rendering_api_helper_is_stubbed():
    """P1-a: a helper with even ONE rendering-API signal (+ low coverage, no
    gameplay) IS stubbed -- the positive signal isolates true rendering
    helpers from menu/save/scene controllers without a class-name list."""
    assert csharp_source_has_rendering_api(_SINGLE_RENDER_CSHARP)
    assert is_input_side_dead(_SINGLE_RENDER_CSHARP)


def test_water_cluster_shapes_still_stub():
    """P1-a regression: the documented water-module shapes (each a single
    ``Shader.`` ref plus other rendering APIs) still stub after the gate now
    requires a positive rendering signal -- the fix narrows menu controllers
    OUT without losing the real rendering helpers."""
    # WaterBase shape (from _RENDER_CSHARP) + the renamed/Displace-style shapes.
    assert is_input_side_dead(_RENDER_CSHARP)            # WaterBase
    assert is_input_side_dead(_RENAMED_RENDER_CSHARP)    # OceanShimmer / GL-only
    displace = (
        "using UnityEngine;\n"
        "public class Displace : MonoBehaviour {\n"
        "  void OnWillRenderObject() {\n"
        "    Shader.EnableKeyword(\"WATER_VERTEX_DISPLACEMENT_ON\");\n"
        "    Shader.DisableKeyword(\"WATER_VERTEX_DISPLACEMENT_OFF\");\n"
        "  }\n}\n"
    )
    assert is_input_side_dead(displace)                  # Displace


# ---------------------------------------------------------------------------
# P2 (re-review): the rendering-API allowlist must cover the broad Unity
# rendering / visual-effect surface, not just the original narrow set. A
# dead-leaning helper using an UNLISTED rendering API (RenderSettings, LensFlare,
# ...) previously slipped the gate -> not stubbed -> generic-mode AI-unavailable
# fail-close. The MenuController guarantee (P1-a) must still hold.
# ---------------------------------------------------------------------------


# A fog / global-render-state helper: RenderSettings.* only (no gameplay).
_FOG_CSHARP = """
using UnityEngine;
public class FogTint : MonoBehaviour {
    void Update() {
        RenderSettings.fogColor = Color.gray;
        RenderSettings.fogDensity = 0.02f;
        RenderSettings.ambientIntensity = 0.5f;
    }
}
"""

# A lens-flare / projector visual helper that makes a REAL rendering CALL
# (``Graphics.DrawMesh``). The ``LensFlare`` / ``Projector`` / ``ReflectionProbe``
# tokens here are type NAMES (declarations / generic args) -- after the R3
# stricter gate they no longer count on their own; the decisive signal is the
# ``Graphics.`` member-access call. No gameplay veto.
_LENSFLARE_CSHARP = """
using UnityEngine;
public class SunGlare : MonoBehaviour {
    void Update() {
        LensFlare flare = GetComponentInChildren<LensFlare>();
        flare.brightness = 1.5f;
        Projector proj = GetComponentInChildren<Projector>();
        ReflectionProbe probe = GetComponentInChildren<ReflectionProbe>();
        Graphics.DrawMesh(mesh, Matrix4x4.identity, mat, 0);
    }
}
"""

# A helper that ONLY declares a serialized rendering-component field and makes no
# rendering CALL. After the R3 stricter gate this is NOT provably dead at
# transpile time (a bare type token in a declaration is not usage) -- the gate
# must leave it alone; the post-coherence detector catches it if its output is
# inert.
_PROJECTOR_FIELD_ONLY_CSHARP = """
using UnityEngine;
public class ShadowCaster : MonoBehaviour {
    public Projector projector;
    [SerializeField] private LensFlare flare;
    void Start() {
        PlayerPrefs.SetInt("shadows", 1);
    }
}
"""

# A MenuController that imports a rendering namespace but never CALLS a rendering
# API. The import line must NOT count as usage.
_MENU_WITH_RENDERING_IMPORT_CSHARP = """
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.Rendering.PostProcessing;
public class MenuController : MonoBehaviour {
    void Start() {
        Cursor.lockState = CursorLockMode.None;
    }
    void OnPlay() {
        PlayerPrefs.SetInt("started", 1);
        SceneManager.LoadScene("Game");
    }
    void OnQuit() {
        Application.Quit();
    }
}
"""

# A live gameplay controller that draws editor-only debug gizmos. ``Gizmos`` /
# ``Handles`` are editor-only (scene-view debugging) and do NOT make the runtime
# module Roblox-dead -- they must NOT be a rendering signal.
_GIZMOS_GAMEPLAY_CSHARP = """
using UnityEngine;
public class PatrolZone : MonoBehaviour {
    public float radius = 5f;
    void Update() {
        PlayerPrefs.SetFloat("radius", radius);
        Application.targetFrameRate = 60;
    }
    void OnDrawGizmos() {
        Gizmos.color = Color.red;
        Gizmos.DrawWireCube(transform.position, Vector3.one * radius);
    }
}
"""


def test_render_settings_helper_is_stubbed():
    """P2: a ``RenderSettings.*`` (fog / ambient) helper -- a previously-UNLISTED
    rendering API -- now produces a positive rendering signal and is stubbed by
    ``is_input_side_dead`` (no gameplay veto + dead-leaning coverage).

    Fail-before-fix CONFIRMED: ``RenderSettings.`` was not in the old
    ``_RENDERING_API_SIGNALS`` list, so ``csharp_source_has_rendering_api``
    returned False -> ``is_input_side_dead`` returned False. The witness below
    shows coverage is dead-leaning with no gameplay veto, so only the missing
    rendering signal kept it un-stubbed.
    """
    # Witness: dead-leaning coverage, no gameplay veto -- only the rendering
    # signal was missing pre-fix.
    assert measure_input_coverage(_FOG_CSHARP).dead_leaning
    assert csharp_source_has_rendering_api(_FOG_CSHARP)
    assert is_input_side_dead(_FOG_CSHARP)


def test_lens_flare_helper_is_stubbed():
    """A visual helper that makes a REAL rendering CALL (``Graphics.DrawMesh``)
    is stubbed -- via the member-access ``Graphics.`` signal, NOT via the
    ``LensFlare`` / ``Projector`` / ``ReflectionProbe`` type NAMES (which the R3
    stricter gate no longer counts on their own)."""
    assert measure_input_coverage(_LENSFLARE_CSHARP).dead_leaning
    assert csharp_source_has_rendering_api(_LENSFLARE_CSHARP)
    assert is_input_side_dead(_LENSFLARE_CSHARP)


def test_menu_controller_still_not_stubbed_after_broadening():
    """Broadening the rendering allowlist must NOT regress the P1-a
    guarantee -- a portable menu/save/scene controller (PlayerPrefs /
    SceneManager / Application.Quit / Cursor, NO rendering APIs) is still NOT
    stubbed at transpile time."""
    assert not csharp_source_has_rendering_api(_MENU_CSHARP)
    assert not is_input_side_dead(_MENU_CSHARP)


# ---------------------------------------------------------------------------
# R3 (Codex review): the destructive transpile-time gate must require ACTUAL
# rendering-API USAGE -- never a bare type token in an import / namespace
# directive or a serialized field declaration. The gate becoming STRICTER is
# SAFE: a falsely-stubbed module silently drops LIVE code, while a missed real
# dead module is still caught by the output-confirmed ``classify_module_dead``.
# ---------------------------------------------------------------------------


def test_menu_controller_with_rendering_import_not_stubbed():
    """R3 P1: a MenuController that only IMPORTS a rendering namespace
    (``using UnityEngine.Rendering.PostProcessing;``) but makes no rendering CALL
    must NOT be stubbed -- the import line is not usage.

    Fail-before-fix CONFIRMED: the OLD signal list had a bare
    ``\\bPostProcess(?:ing)?\\b`` token and stripped only comments, so the
    ``using ...PostProcessing;`` line matched -> ``csharp_source_has_rendering_api``
    returned True -> this live menu controller was stubbed before the AI saw it.
    The witness: coverage is dead-leaning with no gameplay veto, so only the
    (now-removed) import-line match flipped the verdict.
    """
    assert measure_input_coverage(_MENU_WITH_RENDERING_IMPORT_CSHARP).dead_leaning
    assert not csharp_source_has_rendering_api(_MENU_WITH_RENDERING_IMPORT_CSHARP)
    assert not is_input_side_dead(_MENU_WITH_RENDERING_IMPORT_CSHARP)


def test_field_declaration_only_helper_not_stubbed():
    """R3 P1: a script that only DECLARES serialized rendering-component fields
    (``public Projector projector;`` / ``[SerializeField] private LensFlare
    flare;``) and makes no rendering CALL must NOT be stubbed.

    Fail-before-fix CONFIRMED: the OLD signal list had bare ``\\bProjector\\b``
    and ``\\bLensFlare\\b`` tokens, so a field DECLARATION matched ->
    ``csharp_source_has_rendering_api`` returned True -> this module was stubbed
    even though it never calls a rendering API. The R3 gate requires
    member-access / call / lifecycle usage, so a declaration no longer matches.
    """
    assert measure_input_coverage(_PROJECTOR_FIELD_ONLY_CSHARP).dead_leaning
    assert not csharp_source_has_rendering_api(_PROJECTOR_FIELD_ONLY_CSHARP)
    assert not is_input_side_dead(_PROJECTOR_FIELD_ONLY_CSHARP)


def test_type_name_only_helper_still_dead_when_output_inert():
    """R3: dropping the type-NAME-only signals from the DESTRUCTIVE transpile
    gate does NOT weaken the AUTHORITATIVE detector. A field-declaration-only
    helper that the AI transpiles to an inert body is STILL flagged dead by the
    output-confirmed ``classify_module_dead`` -- the post-coherence net catches
    what the (now-stricter) input gate intentionally skips."""
    inert = _INERT_LUAU.replace("WaterBase", "ShadowCaster")
    verdict = classify_module_dead(
        "ShadowCaster",
        csharp_source=_PROJECTOR_FIELD_ONLY_CSHARP,
        luau_source=inert,
    )
    assert verdict.is_dead, verdict.reason
    assert verdict.output_inert
    assert not verdict.vetoed


def test_gizmos_gameplay_controller_not_stubbed():
    """R3 P2: ``Gizmos`` / ``Handles`` are editor-only (scene-view debugging in
    ``OnDrawGizmos`` / ``#if UNITY_EDITOR``) and do NOT make a runtime module
    Roblox-dead. A live gameplay controller with an ``OnDrawGizmos`` body must
    NOT be stubbed at transpile time.

    Fail-before-fix CONFIRMED: the OLD signal list had ``\\bGizmos\\.`` and
    ``\\bHandles\\.``, so ``Gizmos.DrawWireCube(...)`` produced a rendering
    signal -> a low-coverage gameplay controller was stubbed. Removing the
    editor-only tokens fixes it; the witness shows coverage is dead-leaning so
    only the Gizmos signal flipped the verdict pre-fix.
    """
    assert measure_input_coverage(_GIZMOS_GAMEPLAY_CSHARP).dead_leaning
    assert not csharp_source_has_rendering_api(_GIZMOS_GAMEPLAY_CSHARP)
    assert not is_input_side_dead(_GIZMOS_GAMEPLAY_CSHARP)


# ---------------------------------------------------------------------------
# P1-b: prune must protect TRANSITIVE dead deps of a kept-inert module.
# ---------------------------------------------------------------------------


def test_prune_protects_transitive_dead_dep_of_kept_module():
    """P1-b: ``live -> A -> B`` with dead={A,B}. A is kept inert (live caller),
    but A still ``require``s B at module scope -- pruning B would make A's
    require resolve to nil (``require(nil)`` crash). BOTH A and B must be kept
    inert; NOTHING is prunable.

    Fail-before-fix CONFIRMED: the prior direct-caller-only partition returned
    ``prunable={B}, keep_inert={A}`` (B has no DIRECT live caller), which would
    crash A. The transitive-closure partition protects B.
    """
    edges = {"LiveMod": {"A"}, "A": {"B"}, "B": set()}
    result = compute_prunable_dead(frozenset({"A", "B"}), edges)
    assert result.prunable == set(), (
        "B is transitively required by kept-inert A -- must not be pruned"
    )
    assert result.keep_inert == {"A", "B"}


def test_prune_fully_isolated_dead_closure_still_prunable():
    """P1-b: a fully-isolated dead closure (no live requirer ANYWHERE) is
    entirely prunable -- the transitive protection only fires when a live
    module reaches into the closure."""
    edges = {"A": {"B"}, "B": set()}
    result = compute_prunable_dead(frozenset({"A", "B"}), edges)
    assert result.prunable == {"A", "B"}
    assert result.keep_inert == set()


# ---------------------------------------------------------------------------
# P1-c: extract_require_edges recognises non-FindFirstChild lookup shapes.
# ---------------------------------------------------------------------------


def test_waitforchild_require_shape_registers_edge():
    """P1-c: a ``require(...:WaitForChild("Name"))`` form is a real emitted
    module-require shape. The edge must register so a live module requiring a
    dead one via ``WaitForChild`` keeps it inert (not false-pruned).

    Fail-before-fix CONFIRMED: ``_REQUIRE_EDGE`` hardcoded ``FindFirstChild(``,
    so a WaitForChild require returned an empty edge set -> the requirer looked
    like it required nothing -> the dead callee was wrongly prunable.
    """
    src = (
        'local W = require(game:GetService("ReplicatedStorage")'
        ':WaitForChild("WaterBase"))'
    )
    assert extract_require_edges(src, frozenset({"WaterBase"})) == {"WaterBase"}


def test_require_inside_string_literal_is_not_an_edge():
    """PH2 (phase-integration BLOCKING): a ``require(...)`` whose text sits
    INSIDE a string literal is NOT a real edge. ``extract_require_edges``
    stripped comments only, so the require text inside a quoted string
    manufactured a false edge -- on the reachability security path this
    pulled a server-only module into ReplicatedStorage with no real require.

    Single-quoted outer string containing a double-quoted lookup arg.
    FAILS pre-fix (string literals were not stripped).
    """
    src = 'print(\'require(script.Parent:FindFirstChild("Secret"))\')'
    assert extract_require_edges(src, frozenset({"Secret"})) == set()


def test_require_inside_double_quoted_string_is_not_an_edge():
    """PH2 companion: a dotted ``require`` form quoted inside a
    double-quoted string is also not an edge (the inner module-name segment
    must not register)."""
    src = "local msg = \"require(game.ReplicatedStorage.Secret)\""
    assert extract_require_edges(src, frozenset({"Secret"})) == set()


def test_require_inside_long_bracket_string_is_not_an_edge():
    """PH2 companion: a ``require`` inside a long-bracket ``[[ ... ]]``
    string literal is not an edge."""
    src = 'local doc = [[require(script:WaitForChild("Secret"))]]'
    assert extract_require_edges(src, frozenset({"Secret"})) == set()


def test_real_require_with_string_arg_still_registers_after_string_strip():
    """PH2 regression-of-the-regression: stripping outer string literals
    must NOT drop a REAL require whose own argument is a string literal
    (the module name lives inside ``FindFirstChild("Name")``)."""
    src = 'local M = require(script.Parent:FindFirstChild("RealMod"))'
    assert extract_require_edges(src, frozenset({"RealMod"})) == {"RealMod"}


# ---------------------------------------------------------------------------
# Round-4 BLOCKING: a ``--`` INSIDE a string literal must NOT be treated as a
# comment. The old two-pass parser stripped ``--...`` to EOL BEFORE detecting
# string spans, so a ``--`` inside any string TRUNCATED the line and suppressed
# a LATER real ``require(...)`` edge -- dropping a live require-edge (the dead
# partitioner then false-pruned a still-required helper / left a client-required
# helper in ServerStorage). The single string-aware scanner fixes this for every
# string form. Each test below FAILS pre-fix (real edge dropped -> set()).
# ---------------------------------------------------------------------------


def test_dashdash_in_double_quoted_string_keeps_later_require_same_line():
    """A ``--`` inside a double-quoted string is string content, not a comment;
    a real require LATER ON THE SAME LINE still registers."""
    src = (
        'local s = "-- not a comment"; '
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_double_quoted_string_keeps_require_next_line():
    """``--`` inside a double-quoted string does not swallow a require on the
    NEXT line."""
    src = (
        'local s = "-- not a comment"\n'
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_single_quoted_string_keeps_later_require_same_line():
    """``--`` inside a single-quoted string is string content; a real require
    later on the same line still registers."""
    src = (
        "local s = '-- not a comment' "
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_single_quoted_string_keeps_require_next_line():
    """``--`` inside a single-quoted string does not swallow a require on the
    next line."""
    src = (
        "local s = '-- not a comment'\n"
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_long_bracket_string_keeps_later_require_same_line():
    """``--`` inside a ``[[ ... ]]`` long-bracket string is string content; a
    real require later on the same line still registers."""
    src = (
        "local s=[[-- not a comment]] "
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_long_bracket_string_keeps_require_next_line():
    """``--`` inside a ``[[ ... ]]`` long-bracket string does not swallow a
    require on the next line."""
    src = (
        "local s=[[-- not a comment]]\n"
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_dashdash_in_level_long_bracket_string_keeps_require_next_line():
    """``--`` inside a leveled ``[=[ ... ]=]`` long-bracket string is string
    content; a real require on the next line still registers."""
    src = (
        "local s=[=[-- not a comment]=]\n"
        "local M=require(game.ReplicatedStorage.Real)"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == {"Real"}


def test_block_comment_containing_require_is_not_an_edge():
    """A ``--[[ ... ]]`` BLOCK comment containing a ``require(...)`` is a
    comment, not real code -- no edge. (The opener inside the block is comment
    content, never a string.)"""
    src = (
        "--[[ local M=require(game.ReplicatedStorage.Real) ]]\n"
        "local x = 1"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == set()


def test_level_block_comment_containing_require_is_not_an_edge():
    """A leveled ``--[=[ ... ]=]`` block comment containing a ``require(...)``
    is a comment -- no edge."""
    src = (
        "--[=[ local M=require(game.ReplicatedStorage.Real) ]=]\n"
        "local x = 1"
    )
    assert extract_require_edges(src, frozenset({"Real"})) == set()


def test_waitforchild_edge_blocks_false_prune():
    """P1-c end-to-end: a LIVE module requiring a dead one via WaitForChild
    keeps the dead module inert (the edge is visible to the partitioner)."""
    live_src = (
        'local Dead = require(game:GetService("ReplicatedStorage")'
        ':WaitForChild("DeadHelper"))\n'
        "local part = Instance.new('Part')\npart.Parent = workspace"
    )
    scripts = [
        RbxScript(name="LiveLogic", source=live_src, script_type="ModuleScript"),
        RbxScript(name="DeadHelper", source="local M={}\nreturn M",
                  script_type="ModuleScript"),
    ]
    edges = _edges(scripts)
    assert edges["LiveLogic"] == {"DeadHelper"}
    result = compute_prunable_dead(frozenset({"DeadHelper"}), edges)
    assert result.prunable == set()
    assert result.keep_inert == {"DeadHelper"}


# ---------------------------------------------------------------------------
# P2-b: the hard veto catches CHAINED instance property writes.
# ---------------------------------------------------------------------------


def test_chained_property_write_vetoes():
    """P2-b: chained-receiver instance property writes
    (``self.part.CFrame = ...`` / ``workspace.CurrentCamera.FieldOfView = ...``)
    are genuine Roblox effects and must VETO the dead verdict.

    Fail-before-fix CONFIRMED: ``_PROP_WRITE`` only matched ``obj.Prop = ...``
    (a single-segment receiver), so a chained write slipped through and a live
    module mutating Roblox state via a chained write could be flagged dead.
    """
    assert has_genuine_roblox_effect("self.part.CFrame = x")
    assert has_genuine_roblox_effect("workspace.CurrentCamera.FieldOfView = 70")
    # Exclusions still hold: class-table boilerplate, the injected PrimaryPart
    # fixup, and local declarations do NOT veto.
    assert not has_genuine_roblox_effect("WaterBase.__index = WaterBase")
    assert not has_genuine_roblox_effect(
        'self.model.PrimaryPart = self.model:FindFirstChildWhichIsA("BasePart")'
    )
    assert not has_genuine_roblox_effect("local x = a.b")


def test_chained_write_module_is_not_dead():
    """P2-b end-to-end: a module whose only Roblox effect is a chained property
    write is NOT flagged dead (the veto wins)."""
    csharp = _RENDER_CSHARP  # dead-leaning input
    luau = (
        "local M = {}\nM.__index = M\n"
        "function M:Update()\n  self.part.CFrame = CFrame.new(0, 1, 0)\nend\n"
        "return M\n"
    )
    v = classify_module_dead("Mover", csharp_source=csharp, luau_source=luau)
    assert not v.is_dead, v.reason
    assert v.vetoed


# ---------------------------------------------------------------------------
# P2-a: a no-transpile resume REUSES the persisted dead set (does not abstain).
# ---------------------------------------------------------------------------


def test_pipeline_resume_reuses_persisted_dead_set(tmp_path):
    """P2-a: on a no-transpile resume (``transpilation_result is None``) with a
    PERSISTED dead set on the context, the pass reuses it -- so the downstream
    storage classifier keeps the dead module out of ServerStorage instead of
    re-routing it back.

    Fail-before-fix CONFIRMED: before persistence, the resume path returned an
    empty ``dead_modules`` set (abstain), and ``_classify_storage`` re-routed
    the previously-dead module by caller-domain (back into ServerStorage). The
    persisted-set reuse keeps the prior verdict alive across the resume.
    """
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = None
    # Simulate the persisted verdict from the run that transpiled.
    pipeline.ctx.dead_modules = ["WaterBase"]

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset({"WaterBase"})


def test_pipeline_resume_reuse_drops_stale_names(tmp_path):
    """P2-a: a persisted dead name no longer present in the emitted place is
    dropped on reuse (never resurrects a stale verdict for a removed script)."""
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = None
    pipeline.ctx.dead_modules = ["WaterBase", "GoneModule"]

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset({"WaterBase"})


def test_pipeline_persists_dead_set_on_fresh_transpile(tmp_path):
    """P2-a: a fresh transpile persists the computed dead set onto the context
    (``ctx.dead_modules``) so a later no-transpile resume can reuse it.

    Fail-before-fix CONFIRMED: ``ctx.dead_modules`` did not exist; the dead set
    lived only on transient ``state.dead_modules``, lost across a resume."""
    from converter.code_transpiler import TranspilationResult, TranspiledScript
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = TranspilationResult(
        scripts=[
            TranspiledScript(
                source_path="Assets/WaterBase.cs",
                output_filename="WaterBase.luau",
                csharp_source=_RENDER_CSHARP, luau_source=_INERT_LUAU,
                strategy="stub", confidence=1.0, script_type="ModuleScript",
            ),
        ],
        total_transpiled=1, total_ai=0,
    )

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.ctx.dead_modules == ["WaterBase"]


# ---------------------------------------------------------------------------
# P1 (re-review): on resume, REVALIDATE persisted dead verdicts against the
# CURRENT rehydrated Luau body. A still-inert module stays dead (reroute
# preserved); a hand-edited module that now has a genuine Roblox effect is
# DROPPED from the dead set (not pruned, not rerouted-inert) -- the prior
# fix reused the persisted set BY NAME only and clobbered hand-edits.
# ---------------------------------------------------------------------------


def test_pipeline_resume_revalidates_still_inert_module_stays_dead(tmp_path):
    """P1: a persisted-dead module whose rehydrated Luau is STILL inert stays in
    the dead set on resume -- the genuinely-dead reroute is preserved (original
    P2-a goal retained)."""
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=_INERT_LUAU,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = None
    pipeline.ctx.dead_modules = ["WaterBase"]

    pipeline._subphase_analyze_dead_modules()
    assert pipeline.state.dead_modules == frozenset({"WaterBase"})


def test_pipeline_resume_drops_hand_edited_module_from_dead_set(tmp_path):
    """P1: a persisted-dead module whose rehydrated Luau was HAND-EDITED to add a
    genuine Roblox effect (``Instance.new`` / ``.Parent =``) is DROPPED from the
    dead set on resume -- so the prune / reroute-inert consumers never clobber
    the user's edit (supported preserve-scripts / hand-edit workflow).

    Fail-before-fix CONFIRMED: the prior resume path reused the persisted set
    purely BY NAME (filtered only to names still present), so a hand-edited
    still-named ``WaterBase`` stayed flagged dead despite its now-live body. The
    revalidation (output-inert AND not vetoed against the CURRENT body) drops it.
    The prune pass then leaves it in place.
    """
    from core.roblox_types import RbxPlace, RbxScript

    # A body that was hand-edited to add real Roblox logic post-transpile.
    hand_edited = (
        "local WaterBase = {}\n"
        "WaterBase.__index = WaterBase\n"
        "function WaterBase.new()\n"
        "    local part = Instance.new(\"Part\")\n"
        "    part.Parent = workspace\n"
        "    return setmetatable({}, WaterBase)\n"
        "end\n"
        "return WaterBase\n"
    )

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="WaterBase", source=hand_edited,
                  script_type="ModuleScript"),
    ]
    pipeline.state.transpilation_result = None
    pipeline.ctx.dead_modules = ["WaterBase"]

    pipeline._subphase_analyze_dead_modules()
    # Dropped from the dead set: the current body is no longer inert (veto).
    assert pipeline.state.dead_modules == frozenset()

    # And the prune pass leaves the hand-edited module in place (NOT pruned).
    pipeline._subphase_prune_dead_module_closures()
    assert {s.name for s in pipeline.state.rbx_place.scripts} == {"WaterBase"}


# ---------------------------------------------------------------------------
# P3-b: dotted-member method-name tokens do not leak into the bare-type surface.
# ---------------------------------------------------------------------------


def test_dotted_method_name_not_counted_as_bare_type():
    """P3-b: a method name from a dotted call (``EnableKeyword`` from
    ``Shader.EnableKeyword``) must NOT leak into the bare-type surface, where it
    would be measured against TYPE_MAP (no entry) and inflate the unmapped
    count.

    Fail-before-fix CONFIRMED: the prior code stripped only the dotted LEAD
    (``Shader``) from the bare set, leaving ``EnableKeyword`` as a bare token.
    Now the full dotted member chain is stripped.
    """
    from converter.roblox_dead_modules import _extract_csharp_api_refs

    src = (
        "using UnityEngine;\n"
        "public class C : MonoBehaviour {\n"
        "  void F() { Shader.EnableKeyword(\"X\"); }\n}\n"
    )
    dotted, bare = _extract_csharp_api_refs(src)
    assert "Shader.EnableKeyword" in dotted
    assert "EnableKeyword" not in bare
    assert "Shader" not in bare
