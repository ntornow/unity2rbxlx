"""Slice N — the Net: SHAPE-VARIANCE check for the turret child-ref.

Acceptance criterion (e), part 2. Feeds >= 2 DISTINCT faked AI OUTPUT shapes for
the SAME pre-rewritten input and asserts the DURABILITY facts:

  (i) the RESOLVED turret case is AI-SHAPE-INDEPENDENT — because the pre-rewrite
      eliminated the ordinal at the C# level (pre-AI), EVERY valid named-lookup
      output shape yields zero surviving ordinals and the backstop (check D)
      passes. The fix is NOT green on one lucky AI transpile shape.

  (ii) the UNRESOLVED / regressed case is caught by the backstop REGARDLESS of
      which shape the AI emits the positional ordinal in (adjacent
      ``:GetChildren()[1]`` OR the two-line factored ``local k =
      :GetChildren(); k[1]``). Check D fires ``child_ordinal_survivor`` and
      ``fail_closed_errors`` promotes it for BOTH shapes.

Discipline (from ``test_player_shape_corpus.py``): assert on SHAPE FACTS /
load-bearing input invariants and on the verifier's FACT-BASED verdict — NEVER
match one lucky runtime output string. The two output shapes per case differ
only in HOW the named lookup / ordinal is factored; the assertion is the
shape-invariant fact (ordinal absent for resolved; backstop fires for the
regression), which is exactly what AI-shape-independence means.

The whole path is REAL: ``transpile_with_contract`` runs the actual pre-rewrite
+ stamps ``child_ref_resolution``; only ``_ai_transpile`` is faked (no cache, no
real AI call). The verdict comes from the REAL ``verify_contract`` over the
transpiled output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.contract_pipeline import transpile_with_contract  # noqa: E402
from converter.contract_verifier import (  # noqa: E402
    fail_closed_errors,
    verify_contract,
)
from core.roblox_types import RbxScript  # noqa: E402
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402

_GUID = "11111111111111111111111111111111"


# --------------------------------------------------------------------------- #
# Shared synthetic inputs (mirrors the e2e module).
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


def _turret_hierarchy() -> PrefabLibrary:
    """Single-hop turret: Turret -> {Base}. One resolved site keeps the
    survivor-vs-budget arithmetic in the backstop unambiguous ({1,1})."""
    base = _pnode("Base")
    root = _pnode("Turret", children=[base], comp_guid=_GUID)
    return PrefabLibrary(
        prefabs=[
            PrefabTemplate(
                prefab_path=Path("/p/Turret.prefab"), name="Turret", root=root
            )
        ]
    )


def _guid_index(cs_path: Path) -> GuidIndex:
    idx = GuidIndex(project_root=cs_path.parent)
    idx.guid_to_entry[_GUID] = GuidEntry(
        guid=_GUID,
        asset_path=cs_path,
        relative_path=Path(cs_path.name),
        kind="script",
    )
    return idx


# A single transform-rooted GetChild site (resolves to Base) so check D's
# budget is exactly {1,1}: any surviving ordinal is a resolved-site regression.
_TURRET_CS = """\
using UnityEngine;
public class Turret : MonoBehaviour {
    void Fire() { var b = transform.GetChild(0); }
}
"""


def _scene_runtime() -> dict[str, object]:
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


def _drive(tmp_path: Path, fake_luau: str) -> tuple[str, RbxScript]:
    """Drive the REAL generic pipeline on the turret with ``_ai_transpile``
    faked to return ``fake_luau``; return ``(csharp_seen_by_ai, rbx_script)``
    where ``rbx_script`` carries the transpiled source + the
    ``child_ref_resolution`` fact (ready for the real ``verify_contract``)."""
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

    ts = next(
        s for s in result.transpilation.scripts
        if Path(s.source_path).stem == "Turret"
    )
    rbx = RbxScript(
        name="Turret",
        source=ts.luau_source,
        child_ref_resolution=ts.child_ref_resolution,
    )
    return captured["csharp"], rbx


def _verdict(rbx: RbxScript) -> tuple[list[str], list[str], list[str]]:
    """Run the REAL ``verify_contract`` over the script and return
    ``(survivor_checks, gap_checks, fail_closed_errors)``."""
    res = verify_contract({"modules": {"Turret": {"stem": "Turret"}}}, [rbx])
    survivors = [
        v.check for v in res.violations if v.check == "child_ordinal_survivor"
    ]
    gaps = [
        v.check for v in res.violations
        if v.check == "child_ordinal_coverage_gap"
    ]
    return survivors, gaps, fail_closed_errors(res)


# --------------------------------------------------------------------------- #
# (i) RESOLVED case is AI-SHAPE-INDEPENDENT.
#
# Two VALID named-lookup output shapes for the SAME pre-rewritten input:
#   * shape A — adjacent: ``transform:FindFirstChild("Base")``
#   * shape B — factored: ``local b = transform:FindFirstChild("Base"); return b``
# Both must yield zero surviving ordinals + a passing backstop, because the
# ordinal was already gone at the C# level (pre-AI).
# --------------------------------------------------------------------------- #

_RESOLVED_SHAPE_A = """\
local Turret = {}
Turret.__index = Turret
function Turret.new() return setmetatable({}, Turret) end
function Turret:Fire()
    return transform:FindFirstChild("Base")
end
return Turret
"""

_RESOLVED_SHAPE_B = """\
local Turret = {}
Turret.__index = Turret
function Turret.new() return setmetatable({}, Turret) end
function Turret:Fire()
    local b = transform:FindFirstChild("Base")
    return b
end
return Turret
"""

_RESOLVED_SHAPES = pytest.mark.parametrize(
    "fake_luau",
    [
        pytest.param(_RESOLVED_SHAPE_A, id="adjacent-findfirstchild"),
        pytest.param(_RESOLVED_SHAPE_B, id="factored-local-findfirstchild"),
    ],
)


@_RESOLVED_SHAPES
def test_resolved_case_is_ai_shape_independent(
    tmp_path: Path, fake_luau: str
) -> None:
    """For BOTH named-lookup output shapes, the C# the AI saw is ordinal-free
    (the resolved-pre-AI invariant) AND the real backstop emits ZERO survivor /
    zero gap rows. The resolved case never depends on the AI's factoring."""
    csharp_seen, rbx = _drive(tmp_path, fake_luau)

    # Shape-invariant INPUT fact: the ordinal is gone before the AI — so the
    # output is a named lookup regardless of HOW the AI factors it.
    assert 'transform.Find("Base")' in csharp_seen, csharp_seen
    assert ".GetChild(" not in csharp_seen, csharp_seen

    # The stamped fact is fully-resolved {1,1}, and the backstop is silent.
    assert rbx.child_ref_resolution == {
        "getchild_total": 1,
        "resolved_total": 1,
    }
    survivors, gaps, errs = _verdict(rbx)
    assert survivors == [], (
        f"resolved turret must not survive an ordinal in any shape; got {survivors}"
    )
    assert gaps == []
    assert errs == [], f"resolved turret must not fail closed; got {errs}"


def test_resolved_both_shapes_agree_on_no_ordinal(tmp_path: Path) -> None:
    """Cross-shape invariant: feed BOTH shapes and assert they AGREE — neither
    carries a surviving positional ordinal. AI-shape-independence stated as the
    shape FACT both outputs share (not a match on either output string)."""
    _csa, rbx_a = _drive(tmp_path, _RESOLVED_SHAPE_A)
    _csb, rbx_b = _drive(tmp_path, _RESOLVED_SHAPE_B)
    assert ":GetChildren()[" not in rbx_a.source
    assert ":GetChildren()[" not in rbx_b.source
    assert _verdict(rbx_a)[0] == []
    assert _verdict(rbx_b)[0] == []


# --------------------------------------------------------------------------- #
# (ii) UNRESOLVED / regressed case — backstop FIRES regardless of shape.
#
# Same fully-resolved {1,1} pre-rewritten input, but the (faked) AI REGRESSES and
# emits a positional ordinal in two distinct factorings:
#   * shape A — adjacent: ``transform:GetChildren()[1]``
#   * shape B — two-line factored: ``local k = transform:GetChildren(); k[1]``
# A fully-resolved script must NEVER carry an ordinal; check D fail-closes on
# BOTH, proving the backstop is shape-agnostic.
# --------------------------------------------------------------------------- #

_REGRESSED_SHAPE_A = """\
local Turret = {}
Turret.__index = Turret
function Turret.new() return setmetatable({}, Turret) end
function Turret:Fire()
    return transform:GetChildren()[1]
end
return Turret
"""

_REGRESSED_SHAPE_B = """\
local Turret = {}
Turret.__index = Turret
function Turret.new() return setmetatable({}, Turret) end
function Turret:Fire()
    local kids = transform:GetChildren()
    return kids[1]
end
return Turret
"""

_REGRESSED_SHAPES = pytest.mark.parametrize(
    "fake_luau",
    [
        pytest.param(_REGRESSED_SHAPE_A, id="adjacent-getchildren-index"),
        pytest.param(_REGRESSED_SHAPE_B, id="two-line-factored-getchildren"),
    ],
)


@_REGRESSED_SHAPES
def test_backstop_fires_on_surviving_ordinal_any_shape(
    tmp_path: Path, fake_luau: str
) -> None:
    """For a FULLY-RESOLVED ({1,1}) script whose (faked) AI regressed to a
    positional ordinal, the REAL backstop fires ``child_ordinal_survivor`` and
    ``fail_closed_errors`` promotes it — for BOTH the adjacent and the two-line
    factored ordinal shapes. The backstop catches the survivor regardless of how
    the AI factored the ordinal."""
    csharp_seen, rbx = _drive(tmp_path, fake_luau)

    # The pre-rewrite still ran: the C# the AI saw was named, fully-resolved.
    assert 'transform.Find("Base")' in csharp_seen, csharp_seen
    assert rbx.child_ref_resolution == {
        "getchild_total": 1,
        "resolved_total": 1,
    }

    survivors, _gaps, errs = _verdict(rbx)
    assert survivors == ["child_ordinal_survivor"], (
        f"backstop must fire on a surviving ordinal in a fully-resolved script; "
        f"got {survivors}"
    )
    assert any("child_ordinal_survivor" in e for e in errs), (
        f"the survivor must be promoted to a fail-closed error; got {errs}"
    )


def test_unresolved_foreign_receiver_only_info_any_shape(
    tmp_path: Path,
) -> None:
    """The Player-cam coverage-gap case across two ordinal shapes: a script with
    a FOREIGN receiver (``cam = Camera.main.transform``) is unresolved ({1,0}),
    so an ordinal the AI emits — adjacent OR factored — yields ONLY a
    non-promoting ``info`` ``child_ordinal_coverage_gap`` row, never a failure.
    This is the abstain side of the fact-based backstop (the Phase-2 #2-dropped
    ref), shape-independent."""
    player_cs = """\
using UnityEngine;
public class Player : MonoBehaviour {
    Transform cam;
    Transform weaponSlot;
    void Start() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }
}
"""
    guid = "44444444444444444444444444444444"

    def _drive_player(fake_luau: str) -> RbxScript:
        cs = tmp_path / "Player.cs"
        cs.write_text(player_cs, encoding="utf-8")
        infos = [ScriptInfo(path=cs, class_name="Player")]
        # Player host node WITHOUT the cam's child (cam is foreign).
        root = PrefabNode(
            name="Player", file_id="Player", active=True, children=[],
            components=[
                PrefabComponent(
                    component_type="MonoBehaviour",
                    file_id="100",
                    properties={
                        "m_Script": {
                            "fileID": 11500000, "guid": guid, "type": 3
                        }
                    },
                )
            ],
        )
        lib = PrefabLibrary(
            prefabs=[
                PrefabTemplate(
                    prefab_path=Path("/p/Player.prefab"),
                    name="Player",
                    root=root,
                )
            ]
        )
        idx = GuidIndex(project_root=cs.parent)
        idx.guid_to_entry[guid] = GuidEntry(
            guid=guid, asset_path=cs,
            relative_path=Path(cs.name), kind="script",
        )

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
            return fake_luau, 1.0, []

        sr = {
            "modules": {
                "guid-player": {
                    "stem": "Player",
                    "class_name": "Player",
                    "runtime_bearing": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        with patch(
            "converter.code_transpiler._ai_transpile", side_effect=_fake_ai
        ), patch(
            "converter.code_transpiler._find_transpiler",
            return_value="anthropic_api",
        ):
            result = transpile_with_contract(
                str(tmp_path),
                infos,
                scene_runtime=sr,
                use_ai=True,
                api_key="fake-key-for-test",
                parsed_scenes=None,
                prefab_library=lib,
                guid_index=idx,
            )
        ts = next(
            s for s in result.transpilation.scripts
            if Path(s.source_path).stem == "Player"
        )
        return RbxScript(
            name="Player",
            source=ts.luau_source,
            child_ref_resolution=ts.child_ref_resolution,
        )

    player_shape_a = """\
local Player = {}
Player.__index = Player
function Player.new() return setmetatable({}, Player) end
function Player:Start() self.weaponSlot = self.cam:GetChildren()[1] end
return Player
"""
    player_shape_b = """\
local Player = {}
Player.__index = Player
function Player.new() return setmetatable({}, Player) end
function Player:Start()
    local kids = self.cam:GetChildren()
    self.weaponSlot = kids[1]
end
return Player
"""
    for label, shape in (("adjacent", player_shape_a), ("factored", player_shape_b)):
        rbx = _drive_player(shape)
        # Foreign receiver -> unresolved {1,0}; the ordinal stays in C# and
        # survives in the output, but the backstop ABSTAINS (info, not promoted).
        assert rbx.child_ref_resolution == {
            "getchild_total": 1, "resolved_total": 0,
        }, (label, rbx.child_ref_resolution)
        survivors, gaps, errs = _verdict(rbx)
        assert survivors == [], (label, survivors)
        assert gaps == ["child_ordinal_coverage_gap"], (label, gaps)
        assert errs == [], (label, errs)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
