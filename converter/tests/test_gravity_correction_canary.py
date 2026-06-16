"""Phase-2 WIDE-BLAST-RADIUS regression canary (relation #8 scale-faithful gravity).

This is NOT a re-test of Phase-1's single-shape emit behavior
(``test_gravity_correction_emit.py`` already pins the emit gate, the baked scalar,
the mode gate, and idempotency). It pins the genuinely-NEW wide-blast-radius
invariants the single-shape tests cannot give -- proving the injected
``SceneGravityCorrection`` script is a DELIBERATE, expected delta (not a surprise
break) across the full space of dynamic-body shapes a generic conversion produces:

- **Multi-shape anti-vacuity (the core canary):** ONE ``P-mixed`` place carries
  dynamic ``_UnityMass`` bodies of ALL in-scope shapes S1/S2/S4/S5 in a single
  tree; the subphase emits exactly one script AND the fixture provably contains
  each shape (so the assertions cannot pass vacuously on a degraded fixture).
- **C3 script-name symmetric diff:** vs a no-gravity baseline,
  ``SceneGravityCorrection`` is the ONLY added script -- nothing else changes.
- **C4 part-trees unchanged:** the emit subphase appends a script only; it must
  NOT mutate ``workspace_parts`` / ``replicated_templates`` (the VectorForce +
  Attachment are RUNTIME-only Luau instances, never serialized -- design §0.3).
- **C5 prefab-template-only union:** a ``P-prefab`` place whose only dynamic body
  is under ``replicated_templates`` still fires the emit gate (scene ∪ prefab).
- **C-noemit (keyed on ``_UnityMass`` presence, NOT anchored):** ``P-abstain-none``
  (no ``_UnityMass`` anywhere) and ``P-abstain-2d`` (only ``_Rigidbody2D``-flagged
  carriers, incl. a mesh-wrapped 2D) emit NO script.

Each constructed place gets its OWN builder + assertion (no single shared
``_assert_fixture_shapes``). The S2 mesh-wrapped shape is built via the REAL
``_wrap_geometry_with_children_into_model`` producer, so the canary keys off the
deterministic upstream stamping shape rather than a hand-built fingerprint.

The behavioral fall-rate + perf budget (the runtime VectorForce delta CI cannot
see) are the Studio acceptance, run from MAIN (Slice 2.2).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline
from converter.scene_converter import _wrap_geometry_with_children_into_model
from core.roblox_types import RbxConstraint, RbxPart, RbxPlace


_GRAVITY_SCRIPT = "SceneGravityCorrection"


# --------------------------------------------------------------------------
# Low-level builders -- the post-stamp RbxPart shapes the converter produces.
# --------------------------------------------------------------------------
def _s1_basepart(name: str, **attrs: object) -> RbxPart:
    """S1: a non-wrapped dynamic BasePart -- ``_UnityMass`` (+ any 3D facts)
    stamped on the BasePart itself (scene_converter.py:2797/2814)."""
    base: dict[str, object] = {"_UnityMass": 2.0}
    base.update(attrs)
    return RbxPart(name=name, class_name="Part", anchored=False, attributes=base)


def _s2_mesh_wrapped(node_name: str, **outer_attrs: object) -> RbxPart:
    """S2: a mesh-wrapped dynamic body built via the REAL wrap producer.

    Pre-wrap, the geometry BasePart carries ``_UnityMass`` (+ facts) AND a child
    transform; ``_wrap_geometry_with_children_into_model`` then turns the outer
    into a ``Model``, moves ``_UnityMass`` (and ``_Rigidbody2D`` if present) onto a
    synthetic inner ``*_Mesh`` BasePart, and leaves the 3D facts on the outer
    Model -- exactly the real converter output (scene_converter.py:2176-2189)."""
    pre: dict[str, object] = {"_UnityMass": 2.0}
    pre.update(outer_attrs)
    geom = RbxPart(
        name=node_name,
        class_name="MeshPart",
        anchored=False,
        mesh_id="rbxassetid://1",
        children=[RbxPart(name="ChildTransform", class_name="Model")],
        attributes=pre,
    )
    _wrap_geometry_with_children_into_model(geom, node_name)
    assert geom.class_name == "Model", "wrap producer must yield a Model carrier"
    return geom


def _s4_welded_assembly(name: str) -> RbxPart:
    """S4: a welded multi-part assembly -- two dynamic ``_UnityMass`` BaseParts
    joined by the REAL post-conversion weld representation.

    Each member carries its own ``_UnityMass`` (unanchored, scene_converter.py:2797
    per part). The Unity ``FixedJoint`` on member ``_A`` lowers (via
    ``component_converter.convert_joint``) to a
    ``RbxConstraint(constraint_type="WeldConstraint", connected_body_file_id=...)``
    appended to ``_A.constraints``, where ``connected_body_file_id`` resolves to the
    connected body ``_B`` by its Unity ``unity_file_id`` (Part0=owner, Part1=
    connected; resolved at rbxlx-write time)."""
    member_a = _s1_basepart(f"{name}_A")
    member_a.unity_file_id = f"{name}:1"
    member_b = _s1_basepart(f"{name}_B")
    member_b.unity_file_id = f"{name}:2"
    member_a.constraints.append(
        RbxConstraint(
            constraint_type="WeldConstraint",
            connected_body_file_id=member_b.unity_file_id,
        )
    )
    return RbxPart(
        name=name,
        class_name="Model",
        children=[member_a, member_b],
    )


def _humanoid_model(name: str = "Player") -> RbxPart:
    """A character model -- carries NO ``_UnityMass`` (must never be touched)."""
    return RbxPart(
        name=name,
        class_name="Model",
        children=[RbxPart(name="HumanoidRootPart", class_name="Part")],
    )


def _make_generic_pipeline(tmp_path: Path) -> Pipeline:
    """A generic-mode pipeline with the gravity scalar pre-stashed -- the
    ``test_gravity_correction_emit.py`` pattern (drive the real subphase against
    a constructed place; the upstream parse/stash is pinned by Phase-1)."""
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.ctx.scene_runtime_mode = "generic"
    pipeline.ctx.scene_runtime = {"gravityDesiredBaseStuds": 35.03}
    return pipeline


# --------------------------------------------------------------------------
# Tree introspection helpers (recursive, no shared fixture-shape assertion).
# --------------------------------------------------------------------------
def _walk(parts: list[RbxPart]) -> list[RbxPart]:
    out: list[RbxPart] = []
    for p in parts:
        out.append(p)
        out.extend(_walk(p.children))
    return out


def _gravity_scripts(place: RbxPlace) -> list[str]:
    return [s.name for s in place.scripts if s.name == _GRAVITY_SCRIPT]


# --------------------------------------------------------------------------
# Place builders -- each constructed in-test, each with its own anti-vacuity.
# --------------------------------------------------------------------------
def _build_p_mixed(tmp_path: Path) -> Pipeline:
    """P-mixed: ONE workspace tree carrying every in-scope dynamic shape
    (S1/S2/S4/S5) PLUS an anchored ``_UnityMass``-less sibling and a Humanoid
    model (both must survive untouched)."""
    p = _make_generic_pipeline(tmp_path)
    s1 = _s1_basepart("Crate")
    s2 = _s2_mesh_wrapped("Barrel")
    s4 = _s4_welded_assembly("WeldedCrate")
    # S5: an S1 nested under a factless container Model.
    s5 = RbxPart(
        name="ContainerModel",
        class_name="Model",
        children=[_s1_basepart("NestedCrate")],
    )
    anchored_static = RbxPart(name="Floor", class_name="Part", anchored=True)
    p.state.rbx_place.workspace_parts = [
        s1, s2, s4, s5, anchored_static, _humanoid_model()
    ]
    return p


def _build_p_prefab(tmp_path: Path) -> Pipeline:
    """P-prefab: the ONLY dynamic body is under ``replicated_templates``;
    ``workspace_parts`` carries NO ``_UnityMass`` body (scene ∪ prefab union)."""
    p = _make_generic_pipeline(tmp_path)
    p.state.rbx_place.workspace_parts = [
        RbxPart(name="Floor", class_name="Part", anchored=True)
    ]
    p.state.rbx_place.replicated_templates = [_s1_basepart("CrateTemplate")]
    return p


def _build_p_abstain_none(tmp_path: Path) -> Pipeline:
    """P-abstain-none: NO ``_UnityMass`` anywhere -- only static geometry and a
    Humanoid model (the all-anchored / static case, design §0.5)."""
    p = _make_generic_pipeline(tmp_path)
    p.state.rbx_place.workspace_parts = [
        RbxPart(name="Floor", class_name="Part", anchored=True),
        _humanoid_model(),
    ]
    return p


def _build_p_abstain_2d(tmp_path: Path) -> Pipeline:
    """P-abstain-2d: the only ``_UnityMass`` carriers are ``_Rigidbody2D``-flagged
    (incl. a mesh-wrapped 2D whose inner carrier holds BOTH ``_UnityMass`` and
    ``_Rigidbody2D``, co-located via the move-list). Physics2D is OOS -> no emit."""
    p = _make_generic_pipeline(tmp_path)
    flat_2d = _s1_basepart("Coin2D", _Rigidbody2D=True)
    wrapped_2d = _s2_mesh_wrapped("Wheel2D", _Rigidbody2D=True)
    p.state.rbx_place.workspace_parts = [flat_2d, wrapped_2d]
    return p


# --------------------------------------------------------------------------
# AC-2.6 -- multi-shape anti-vacuity: P-mixed provably contains each shape.
# --------------------------------------------------------------------------
class TestPMixedAntiVacuity:
    """The fixture must provably contain >=1 each of S1/S2/S4/S5 dynamic
    ``_UnityMass`` carriers (plus the anchored + Humanoid siblings), so the
    C1/C3/C4 assertions cannot pass vacuously on a degraded fixture."""

    def test_fixture_contains_all_dynamic_shapes(self, tmp_path: Path) -> None:
        p = _build_p_mixed(tmp_path)
        parts = _walk(p.state.rbx_place.workspace_parts)
        by_name = {p_.name: p_ for p_ in parts}

        # S1: a non-wrapped dynamic BasePart with _UnityMass on itself.
        s1 = by_name["Crate"]
        assert s1.class_name == "Part" and not s1.anchored
        assert isinstance(s1.attributes.get("_UnityMass"), float)
        assert s1.attributes.get("_Rigidbody2D") is None

        # S2: a mesh-wrapped body -- outer Model, inner *_Mesh carries _UnityMass.
        outer = by_name["Barrel"]
        assert outer.class_name == "Model"
        inner = by_name["Barrel_Mesh"]
        assert inner.class_name == "MeshPart"
        assert isinstance(inner.attributes.get("_UnityMass"), float)
        assert "_UnityMass" not in outer.attributes  # moved to inner by the producer

        # S4: a welded assembly -- >=2 dynamic _UnityMass BaseParts JOINED by a
        # real WeldConstraint. Assert the weld RELATION exists (a WeldConstraint
        # linking the members by the connected body's unity_file_id), not merely
        # that the member names are present -- so a regression in the welded-
        # assembly representation (dropped/mis-typed constraint) REDs this canary.
        welded_members = [
            x for x in parts
            if x.name.startswith("WeldedCrate_")
            and isinstance(x.attributes.get("_UnityMass"), float)
        ]
        assert len(welded_members) >= 2
        member_ids = {
            x.unity_file_id for x in welded_members if x.unity_file_id is not None
        }
        welds = [
            c
            for x in welded_members
            for c in x.constraints
            if c.constraint_type == "WeldConstraint"
        ]
        assert welds, "S4 must carry a WeldConstraint joining its members"
        assert any(
            w.connected_body_file_id in member_ids for w in welds
        ), "the WeldConstraint must reference a sibling member as its connected body"

        # S5: a dynamic BasePart nested under a factless container Model.
        container = by_name["ContainerModel"]
        assert container.class_name == "Model"
        assert "_UnityMass" not in container.attributes  # factless container
        nested = by_name["NestedCrate"]
        assert isinstance(nested.attributes.get("_UnityMass"), float)
        assert nested in container.children

        # Non-counting siblings: an anchored static part and a Humanoid model,
        # neither carrying _UnityMass.
        floor = by_name["Floor"]
        assert floor.anchored and "_UnityMass" not in floor.attributes
        assert "Player" in by_name and "HumanoidRootPart" in by_name

        # The gate predicate must agree the place is a positive-emit context.
        assert Pipeline._part_tree_has_dynamic_unitymass(
            p.state.rbx_place.workspace_parts
        ) is True

    def test_anti_vacuity_has_teeth_emit_stops_if_shapes_removed(
        self, tmp_path: Path
    ) -> None:
        """The canary must RED if the fixture silently loses its dynamic bodies:
        a place with the dynamic carriers stripped emits NO script (so a degraded
        fixture cannot pass C1 hollow)."""
        p = _build_p_mixed(tmp_path)
        # Strip every _UnityMass carrier (simulate a refactor dropping dynamics).
        for part in _walk(p.state.rbx_place.workspace_parts):
            part.attributes.pop("_UnityMass", None)
        p._subphase_inject_gravity_correction()
        assert _gravity_scripts(p.state.rbx_place) == []


# --------------------------------------------------------------------------
# AC-2.2 -- C1 (delta present, exactly once) + C3 (script-name symmetric diff).
# --------------------------------------------------------------------------
class TestPMixedScriptDelta:
    def test_c1_exactly_one_gravity_script_to_server(self, tmp_path: Path) -> None:
        p = _build_p_mixed(tmp_path)
        p._subphase_inject_gravity_correction()
        sgc = [
            s for s in p.state.rbx_place.scripts if s.name == _GRAVITY_SCRIPT
        ]
        assert len(sgc) == 1
        script = sgc[0]
        assert script.parent_path == "ServerScriptService"
        assert script.script_type == "Script"
        assert (
            "-- SceneGravityCorrection (auto-generated; relation #8" in script.source
        )

    def test_c3_gravity_script_is_only_added_script(self, tmp_path: Path) -> None:
        """vs a no-gravity baseline (same place, no dynamic _UnityMass), the
        symmetric difference of script NAMES is EXACTLY {SceneGravityCorrection}
        -- nothing else appears or vanishes."""
        # Seed a pre-existing unrelated script on BOTH runs so the symmetric diff
        # genuinely tests "only the gravity script changed", not "the set was
        # empty before".
        def _seed_unrelated(pl: Pipeline) -> None:
            from core.roblox_types import RbxScript
            pl.state.rbx_place.scripts.append(
                RbxScript(name="ExistingModule", source="-- unrelated")
            )

        # Baseline: the SAME mixed tree but with no dynamic _UnityMass -> no emit.
        baseline = _build_p_mixed(tmp_path / "baseline")
        for part in _walk(baseline.state.rbx_place.workspace_parts):
            part.attributes.pop("_UnityMass", None)
        _seed_unrelated(baseline)
        before = {s.name for s in baseline.state.rbx_place.scripts}
        baseline._subphase_inject_gravity_correction()
        baseline_names = {s.name for s in baseline.state.rbx_place.scripts}
        assert baseline_names == before  # baseline truly emits nothing

        # With dynamics present, exactly one new name appears.
        active = _build_p_mixed(tmp_path / "active")
        _seed_unrelated(active)
        names_before = {s.name for s in active.state.rbx_place.scripts}
        active._subphase_inject_gravity_correction()
        names_after = {s.name for s in active.state.rbx_place.scripts}
        assert names_after ^ names_before == {_GRAVITY_SCRIPT}


# --------------------------------------------------------------------------
# AC-2.3 -- C4: the emit subphase does NOT mutate the part trees.
# --------------------------------------------------------------------------
class TestPMixedNoTreeMutation:
    def test_c4_workspace_and_templates_unchanged(self, tmp_path: Path) -> None:
        """The VectorForce + Attachment children are RUNTIME-only (design §0.3);
        the subphase appends a script and must not touch either part tree. The
        anchored sibling survives unchanged."""
        p = _build_p_mixed(tmp_path)
        p.state.rbx_place.replicated_templates = [_s1_basepart("CrateTemplate")]

        ws_before = copy.deepcopy(p.state.rbx_place.workspace_parts)
        tmpl_before = copy.deepcopy(p.state.rbx_place.replicated_templates)

        p._subphase_inject_gravity_correction()

        # A script WAS emitted (so C4 is not vacuously testing a no-op subphase).
        assert _gravity_scripts(p.state.rbx_place) == [_GRAVITY_SCRIPT]
        assert p.state.rbx_place.workspace_parts == ws_before
        assert p.state.rbx_place.replicated_templates == tmpl_before


# --------------------------------------------------------------------------
# AC-2.4 -- C5: the prefab-template-only union fires the emit gate.
# --------------------------------------------------------------------------
class TestPPrefabUnion:
    def test_c5_prefab_template_only_still_emits(self, tmp_path: Path) -> None:
        p = _build_p_prefab(tmp_path)
        # Anti-vacuity: workspace has NO dynamic body; the template DOES.
        assert Pipeline._part_tree_has_dynamic_unitymass(
            p.state.rbx_place.workspace_parts
        ) is False
        assert Pipeline._part_tree_has_dynamic_unitymass(
            p.state.rbx_place.replicated_templates
        ) is True

        p._subphase_inject_gravity_correction()
        assert _gravity_scripts(p.state.rbx_place) == [_GRAVITY_SCRIPT]


# --------------------------------------------------------------------------
# AC-2.5 -- C-noemit: deliberate ABSTAIN keyed on _UnityMass presence.
# --------------------------------------------------------------------------
class TestNoEmitCases:
    def test_p_abstain_none_emits_nothing(self, tmp_path: Path) -> None:
        p = _build_p_abstain_none(tmp_path)
        # Anti-vacuity: the fixture genuinely has NO _UnityMass anywhere.
        assert Pipeline._part_tree_has_dynamic_unitymass(
            p.state.rbx_place.workspace_parts
        ) is False
        p._subphase_inject_gravity_correction()
        assert _gravity_scripts(p.state.rbx_place) == []

    def test_p_abstain_2d_emits_nothing(self, tmp_path: Path) -> None:
        p = _build_p_abstain_2d(tmp_path)
        parts = _walk(p.state.rbx_place.workspace_parts)
        by_name = {p_.name: p_ for p_ in parts}

        # Anti-vacuity: the flat 2D carrier holds BOTH _UnityMass and _Rigidbody2D.
        coin = by_name["Coin2D"]
        assert isinstance(coin.attributes.get("_UnityMass"), float)
        assert coin.attributes.get("_Rigidbody2D") is True

        # And the mesh-wrapped 2D: the inner carrier holds BOTH (co-located via
        # the move-list), proving the 2D discriminator travels with _UnityMass.
        inner = by_name["Wheel2D_Mesh"]
        assert isinstance(inner.attributes.get("_UnityMass"), float)
        assert inner.attributes.get("_Rigidbody2D") is True

        assert Pipeline._part_tree_has_dynamic_unitymass(
            p.state.rbx_place.workspace_parts
        ) is False
        p._subphase_inject_gravity_correction()
        assert _gravity_scripts(p.state.rbx_place) == []
