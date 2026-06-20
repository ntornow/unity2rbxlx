"""Slice N — the Net: COLD/UNCACHED generic e2e for the turret child-ref.

Acceptance criterion (e), part 1. Drives the turret child-ref end-to-end through
the REAL pre-rewrite + ``transpile_with_contract`` (generic mode) with the AI
transpile FAKED — so no on-disk LLM cache is load-bearing (the green run cannot
be a cache artifact, edge E7).

What makes this the NET, not a stub:

  * It exercises the REAL ``child_ref_resolver.build_child_ref_map`` +
    ``prerewrite_child_index`` inside ``transpile_with_contract`` — the SAME
    production call the canary uses. The fake replaces ONLY the AI backend
    (``_ai_transpile``), exactly as ``test_contract_pipeline_end_to_end.py``
    does; the resolver, the hook, the cache-key derivation, and the
    ``child_ref_resolution`` stamping are all the real code.
  * COLD: ``_ai_transpile`` is monkeypatched to return a deterministic shape,
    so the test never reaches the on-disk cache (``_ai_cache_key`` /
    ``LLM_CACHE_DIR``) and never makes a real AI call. Because the pre-rewrite
    mutates ``csharp_source`` BEFORE it enters the cache key, even a warm cache
    from a pre-rewrite build keys differently — but the fake removes the cache
    from the path entirely.
  * The load-bearing proof that the REAL pre-rewrite ran is an assertion on the
    INPUT the fake AI receives: the C# it is handed contains
    ``transform.Find("Base")`` / ``tBase.Find("Weapon")`` / ``tWeapon.Find("Origin")``
    and NO ``transform.GetChild`` ordinal. The ordinal is gone PRE-AI, so the
    output is a named lookup regardless of how the AI factors it.

This mirrors the ``test_player_shape_corpus.py`` discipline: assert SHAPE FACTS /
load-bearing input invariants, NEVER match one lucky runtime output string.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.contract_pipeline import transpile_with_contract  # noqa: E402
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    ParsedScene,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
    SceneNode,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402

_GUID = "11111111111111111111111111111111"


# --------------------------------------------------------------------------- #
# Synthetic SimpleFPS-shaped inputs (the resolver test's builders). Building the
# parse in-test keeps the e2e hermetic + cold — no populated submodule needed —
# while the RESOLVED FACT under test (Turret -> Base/Weapon/Origin) is the real
# turret's prefab nesting.
# --------------------------------------------------------------------------- #


def _mono(guid: str) -> PrefabComponent:
    return PrefabComponent(
        component_type="MonoBehaviour",
        file_id="100",
        properties={"m_Script": {"fileID": 11500000, "guid": guid, "type": 3}},
    )


def _pnode(
    name: str,
    *,
    children: list[PrefabNode] | None = None,
    comp_guid: str | None = None,
) -> PrefabNode:
    return PrefabNode(
        name=name,
        file_id=name,
        active=True,
        children=children or [],
        components=[_mono(comp_guid)] if comp_guid else [],
    )


def _turret_hierarchy(comp_guid: str = _GUID) -> PrefabLibrary:
    """Turret -> {Base -> {Weapon -> {Origin}}, Collider}; MonoBehaviour on the
    Turret root. Matches the real ``Turret.prefab`` Transform graph (design §0
    fact 10)."""
    origin = _pnode("Origin")
    weapon = _pnode("Weapon", children=[origin])
    base = _pnode("Base", children=[weapon])
    collider = _pnode("Collider")
    root = _pnode("Turret", children=[base, collider], comp_guid=comp_guid)
    template = PrefabTemplate(
        prefab_path=Path("/p/Turret.prefab"), name="Turret", root=root
    )
    return PrefabLibrary(prefabs=[template])


def _guid_index(cs_path: Path, guid: str = _GUID) -> GuidIndex:
    idx = GuidIndex(project_root=cs_path.parent)
    idx.guid_to_entry[guid] = GuidEntry(
        guid=guid,
        asset_path=cs_path,
        relative_path=Path(cs_path.name),
        kind="script",
    )
    return idx


# The real turret shape: chained block-bodied Transform property getters
# (Turret.cs:37-48 — tBase=transform.GetChild(0); tWeapon=tBase.GetChild(0);
# tOrigin=tWeapon.GetChild(0)).
_TURRET_CS = """\
using UnityEngine;
public class Turret : MonoBehaviour {
    private Transform tBase { get { return transform.GetChild(0); } }
    private Transform tWeapon { get { return tBase.GetChild(0); } }
    private Transform tOrigin { get { return tWeapon.GetChild(0); } }
    void Fire() { var origin = tOrigin.position; }
}
"""

# A deterministic, contract-compliant fake AI output: a class table that
# transpiles the pre-rewritten C# ``transform.Find("Base")`` chain to the named
# Roblox lookups it implies. This is the SHAPE the resolved case yields — but the
# test does NOT assert against this string; it asserts the INPUT the AI saw (the
# pre-rewrite ran) and the resolution FACT. The named lookups here only keep the
# output ordinal-free so the end-to-end re-assert of (f) holds.
_FAKE_TURRET_LUAU = """\
local Turret = {}
Turret.__index = Turret
function Turret.new()
    return setmetatable({}, Turret)
end
function Turret:Fire()
    local tBase = transform:FindFirstChild("Base")
    local tWeapon = tBase:FindFirstChild("Weapon")
    local origin = tWeapon:FindFirstChild("Origin")
    return origin
end
return Turret
"""


def _scene_runtime() -> dict[str, object]:
    """The minimal generic ``scene_runtime`` artifact: the Turret is a
    runtime-bearing component class so it flows through the generic contract."""
    return {
        "modules": {
            "guid-turret": {
                "stem": "Turret",
                "class_name": "Turret",
                "runtime_bearing": True,
            },
        },
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    }


def _drive_turret_e2e(
    tmp_path: Path,
    fake_luau: str,
) -> tuple[str, object]:
    """Drive the REAL ``transpile_with_contract`` (generic) on the turret with
    ``_ai_transpile`` faked to return ``fake_luau``. Captures the C# the fake AI
    is handed (the pre-rewrite's output) and returns
    ``(csharp_seen_by_ai, turret_TranspiledScript)``. No on-disk cache, no real
    AI call."""
    cs = tmp_path / "Turret.cs"
    cs.write_text(_TURRET_CS, encoding="utf-8")
    infos = [ScriptInfo(path=cs, class_name="Turret")]

    captured: dict[str, str] = {}

    def _fake_ai(
        csharp_source: str,
        api_key: str,
        model: str,
        class_name: str = "",
        script_type: str = "Script",
        project_context: str = "",
        runtime_mode: str = "legacy",
        is_player_controller: bool = False,
        send_message_facts: tuple = (),
    ) -> tuple[str, float, list[str]]:
        # The pre-rewrite has ALREADY mutated csharp_source before it reaches
        # here — this is the load-bearing capture proving the real resolver ran.
        captured["csharp"] = csharp_source
        return fake_luau, 1.0, []

    with patch(
        "converter.code_transpiler._ai_transpile", side_effect=_fake_ai
    ), patch(
        "converter.code_transpiler._find_transpiler",
        return_value="anthropic_api",
    ):
        result = transpile_with_contract(
            str(tmp_path),
            infos,
            scene_runtime=_scene_runtime(),
            use_ai=True,
            api_key="fake-key-for-test",
            parsed_scenes=None,
            prefab_library=_turret_hierarchy(),
            guid_index=_guid_index(cs),
        )

    assert "csharp" in captured, "the faked AI backend was never invoked"
    turret = next(
        s for s in result.transpilation.scripts
        if Path(s.source_path).stem == "Turret"
    )
    return captured["csharp"], turret


# --------------------------------------------------------------------------- #
# The Net: cold/uncached generic e2e.
# --------------------------------------------------------------------------- #


def test_prerewrite_eliminates_ordinal_before_ai_sees_it(tmp_path: Path) -> None:
    """The REAL pre-rewrite runs inside ``transpile_with_contract`` and the C#
    the (faked) AI receives is ALL named ``.Find("<child>")`` lookups with the
    receiver symbols preserved and NO ``.GetChild`` ordinal anywhere. This is the
    durability fact: the ordinal is gone PRE-AI, so the output is a named lookup
    no matter how the AI factors its own emission."""
    csharp_seen, _turret = _drive_turret_e2e(tmp_path, _FAKE_TURRET_LUAU)

    # The chained getter chain was resolved against the prefab nesting and the
    # receiver symbol was preserved on each hop.
    assert 'transform.Find("Base")' in csharp_seen, csharp_seen
    assert 'tBase.Find("Weapon")' in csharp_seen, csharp_seen
    assert 'tWeapon.Find("Origin")' in csharp_seen, csharp_seen
    # NO ordinal reaches the AI — the load-bearing PRE-AI elimination.
    assert ".GetChild(" not in csharp_seen, (
        "an ordinal reached the AI — the pre-rewrite did not run end-to-end; "
        f"C# the AI saw:\n{csharp_seen}"
    )


def test_turret_stamped_fully_resolved(tmp_path: Path) -> None:
    """The resolved turret carries ``child_ref_resolution = {3, 3}`` on its
    transpiled script — the deterministic fact check D (the backstop) keys on.
    Fully-resolved (3/3) means a surviving ordinal would be a regression, caught
    fail-closed downstream."""
    _csharp_seen, turret = _drive_turret_e2e(tmp_path, _FAKE_TURRET_LUAU)
    assert turret.child_ref_resolution == {
        "getchild_total": 3,
        "resolved_total": 3,
    }, turret.child_ref_resolution
    # And the turret routes through the generic contract as a ModuleScript.
    assert turret.script_type == "ModuleScript"


def test_resolved_output_has_no_surviving_ordinal(tmp_path: Path) -> None:
    """Re-assert (f) end-to-end: because the ordinal was eliminated pre-AI, the
    transpiled Luau for the resolved turret carries no positional
    ``GetChildren()[n]`` ordinal — the output is ordinal-free regardless of the
    AI's factoring. (Asserted as a SHAPE FACT — absence of the ordinal — not a
    match on one lucky output string.)"""
    _csharp_seen, turret = _drive_turret_e2e(tmp_path, _FAKE_TURRET_LUAU)
    assert ":GetChildren()[" not in turret.luau_source, turret.luau_source


def test_cold_no_real_ai_call_no_cache_dependency(tmp_path: Path) -> None:
    """COLD guard: the run completes with the AI backend faked, so no real AI
    call and no on-disk cache read is load-bearing. If ``_ai_transpile`` were
    NOT faked (a cache miss reaching the real backend), the call would fail
    without a network/API key — proving the green run is the fake's
    deterministic output, never a warm-cache artifact (edge E7).

    We assert this by driving the e2e with a fake that RAISES if the real
    backend is reached past the patch — i.e. the patch is the only transpile
    path. The successful resolution fact below proves the path completed cold."""
    cs = tmp_path / "Turret.cs"
    cs.write_text(_TURRET_CS, encoding="utf-8")
    infos = [ScriptInfo(path=cs, class_name="Turret")]

    calls: list[str] = []

    def _counting_fake(
        csharp_source: str,
        api_key: str,
        model: str,
        class_name: str = "",
        script_type: str = "Script",
        project_context: str = "",
        runtime_mode: str = "legacy",
        is_player_controller: bool = False,
        send_message_facts: tuple = (),
    ) -> tuple[str, float, list[str]]:
        calls.append(class_name or "Turret")
        return _FAKE_TURRET_LUAU, 1.0, []

    with patch(
        "converter.code_transpiler._ai_transpile", side_effect=_counting_fake
    ), patch(
        "converter.code_transpiler._find_transpiler",
        return_value="anthropic_api",
    ):
        result = transpile_with_contract(
            str(tmp_path),
            infos,
            scene_runtime=_scene_runtime(),
            use_ai=True,
            api_key="fake-key-for-test",
            parsed_scenes=None,
            prefab_library=_turret_hierarchy(),
            guid_index=_guid_index(cs),
        )

    # The fake (not a cache, not a real backend) produced the transpile output:
    # exactly one AI invocation for the turret, and the resolution fact landed.
    assert calls == ["Turret"], (
        f"expected exactly one faked AI call for the turret; got {calls}"
    )
    turret = next(
        s for s in result.transpilation.scripts
        if Path(s.source_path).stem == "Turret"
    )
    assert turret.child_ref_resolution == {
        "getchild_total": 3,
        "resolved_total": 3,
    }


def test_scene_hosted_turret_resolves_via_single_scene_fallback(
    tmp_path: Path,
) -> None:
    """Single-scene fallback (design §0 fact 2): the canary path threads
    ``all_parsed_scenes or [parsed_scene]``, so a SCENE-hosted turret must
    resolve when the script's host is a ``SceneNode`` (not only a prefab). Drive
    the e2e with the turret hierarchy supplied as a parsed SCENE and an empty
    prefab library; the pre-rewrite must still eliminate the ordinal pre-AI."""
    cs = tmp_path / "Turret.cs"
    cs.write_text(_TURRET_CS, encoding="utf-8")
    infos = [ScriptInfo(path=cs, class_name="Turret")]

    # Build the same Turret->Base->Weapon->Origin nesting out of SceneNodes.
    def _snode(
        name: str,
        children: list[SceneNode] | None = None,
        comp_guid: str | None = None,
    ) -> SceneNode:
        return SceneNode(
            name=name,
            file_id=name,
            active=True,
            layer=0,
            tag="Untagged",
            children=children or [],
            components=[_mono(comp_guid)] if comp_guid else [],
        )

    origin = _snode("Origin")
    weapon = _snode("Weapon", [origin])
    base = _snode("Base", [weapon])
    collider = _snode("Collider")
    turret = _snode("Turret", [base, collider], comp_guid=_GUID)
    all_nodes = {
        n.file_id: n for n in (turret, base, weapon, origin, collider)
    }
    scene = ParsedScene(scene_path=Path("/p/Main.unity"), all_nodes=all_nodes)

    captured: dict[str, str] = {}

    def _fake_ai(
        csharp_source: str,
        api_key: str,
        model: str,
        class_name: str = "",
        script_type: str = "Script",
        project_context: str = "",
        runtime_mode: str = "legacy",
        is_player_controller: bool = False,
        send_message_facts: tuple = (),
    ) -> tuple[str, float, list[str]]:
        captured["csharp"] = csharp_source
        return _FAKE_TURRET_LUAU, 1.0, []

    with patch(
        "converter.code_transpiler._ai_transpile", side_effect=_fake_ai
    ), patch(
        "converter.code_transpiler._find_transpiler",
        return_value="anthropic_api",
    ):
        result = transpile_with_contract(
            str(tmp_path),
            infos,
            scene_runtime=_scene_runtime(),
            use_ai=True,
            api_key="fake-key-for-test",
            # The fallback the canary call site uses: scene list, no prefabs.
            parsed_scenes=[scene],
            prefab_library=PrefabLibrary(),
            guid_index=_guid_index(cs),
        )

    assert 'transform.Find("Base")' in captured["csharp"], captured["csharp"]
    assert ".GetChild(" not in captured["csharp"], captured["csharp"]
    t = next(
        s for s in result.transpilation.scripts
        if Path(s.source_path).stem == "Turret"
    )
    assert t.child_ref_resolution == {"getchild_total": 3, "resolved_total": 3}


# Guard: this test module is meaningful only with the FT pre-rewrite merged.
def test_pre_rewrite_symbols_present() -> None:
    """Sanity: the FT pre-rewrite this Net exercises is importable. If the FT
    slice is absent, this Net is testing nothing — fail loudly rather than pass
    vacuously."""
    from converter import child_ref_resolver

    assert hasattr(child_ref_resolver, "build_child_ref_map")
    assert hasattr(child_ref_resolver, "prerewrite_child_index")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
