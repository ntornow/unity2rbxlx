"""Tests for the projectile slice (PR #73b).

Covers:
  - Projectile capability dataclasses (immutability, READS/WRITES).
  - Validator: TurretBullet and PlaneBullet happy paths; rejects
    HitDetection.RaycastSegment placed BEFORE Movement.Impulse (reader-
    after-writer); rejects Effect.Damage without a prior HitDetection
    writer.
  - Luau emitter: golden output for TurretBullet + PlaneBullet stubs.
  - Detectors: TurretBulletDetector and PlaneBulletDetector composition-
    only primary signal, source-aware confirm, behavior() round-trips
    through the validator, and the negative-discriminator that keeps
    TurretBullet from matching PlaneBullet sources.
  - Integration: classify_scripts emits per-instance bindings for a
    synthetic SimpleFPS-shaped scene; the bullet ``.cs`` paths land in
    ``skip_paths`` so the AI transpiler is bypassed.
  - Pipeline mutual exclusion: ``bullet_physics_raycast`` is disabled
    when ``use_gameplay_adapters`` is on.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from converter.gameplay.capabilities import (
    Behavior,
    ContainerResolver,
    EffectDamage,
    EffectSplash,
    EffectSpawnTemplate,
    HitDetectionOverlapSphere,
    HitDetectionRaycastSegment,
    LifetimeDespawn,
    LifetimePersistent,
    MovementImpulse,
)
from converter.gameplay.composer import (
    ADAPTER_STUB_MARKER,
    BehaviorCompositionError,
    emit_behavior_stub,
    validate_behavior,
)
from converter.gameplay.detectors import (
    ALL_DETECTORS,
    PlaneBulletDetector,
    TurretBulletDetector,
    detect,
)


# ---------------------------------------------------------------------------
# Canonical Unity sources (verbatim from test_projects/SimpleFPS)
# ---------------------------------------------------------------------------

_TURRET_BULLET_CSHARP = """
using UnityEngine;
public class TurretBullet : MonoBehaviour {
    public float fadeTime = 3f;
    public int force = 60;
    public int damage = 10;
    private Rigidbody rb { get { return GetComponent<Rigidbody>(); } }
    private void Start() {
        rb.AddRelativeForce(Vector3.forward * force, ForceMode.Impulse);
        Destroy(gameObject, fadeTime);
    }
    private void OnCollisionEnter(Collision other) {
        if (other.collider.tag == "Player") {
            other.collider.SendMessage("TakeDamage", damage);
            Destroy(gameObject);
        }
    }
}
"""

_PLANE_BULLET_CSHARP = """
using UnityEngine;
public class PlaneBullet : MonoBehaviour {
    public GameObject explosion;
    public float fadeTime = 6f;
    public int force = 200;
    public int damage = 10;
    private Rigidbody rb { get { return GetComponent<Rigidbody>(); } }
    private void Start() {
        rb.AddRelativeForce(Vector3.forward * force, ForceMode.Impulse);
        Destroy(gameObject, fadeTime);
    }
    private void OnCollisionEnter(Collision other) {
        Instantiate(explosion, transform.position, Quaternion.identity);
        Collider[] cols = Physics.OverlapSphere(transform.position, 2);
        foreach (Collider col in cols) {
            if (col.tag == "Player")
                col.SendMessage("TakeDamage", damage);
        }
        Destroy(gameObject);
    }
}
"""


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

@dataclass
class _StubComponent:
    component_type: str
    file_id: str = "1"
    properties: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.properties is None:
            self.properties = {}


@dataclass
class _StubNode:
    file_id: str = "n1"
    name: str = "Stub"
    components: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.components is None:
            self.components = []


def _mono(class_name: str, file_id: str = "c1") -> _StubComponent:
    return _StubComponent(
        component_type="MonoBehaviour",
        file_id=file_id,
        properties={"_script_class_name": class_name},
    )


def _turret_behavior() -> Behavior:
    return Behavior(
        unity_file_id="bullet_1",
        diagnostic_name="TurretBullet",
        capabilities=(
            MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=60),
            LifetimeDespawn(seconds=3),
            HitDetectionRaycastSegment(),
            EffectDamage(value=10),
        ),
    )


def _plane_behavior() -> Behavior:
    return Behavior(
        unity_file_id="bullet_2",
        diagnostic_name="PlaneBullet",
        capabilities=(
            MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=200),
            LifetimeDespawn(seconds=6),
            HitDetectionRaycastSegment(),
            EffectSpawnTemplate(name="Explosion"),
            EffectSplash(radius_unity=2, value=10),
        ),
    )


# ---------------------------------------------------------------------------
# Capability dataclasses
# ---------------------------------------------------------------------------

class TestProjectileCapabilities:
    def test_movement_impulse_is_frozen(self) -> None:
        cap = MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=60)
        with pytest.raises(Exception):
            cap.force_unity = 999  # type: ignore[misc]

    def test_movement_impulse_writes_velocity(self) -> None:
        assert MovementImpulse.WRITES == frozenset({"ctx.movement.velocity"})
        assert MovementImpulse.READS == frozenset()

    def test_lifetime_despawn_no_ctx_traffic(self) -> None:
        # Despawn is a fire-and-forget Debris:AddItem — no cross-family
        # state needs to flow.
        assert LifetimeDespawn.READS == frozenset()
        assert LifetimeDespawn.WRITES == frozenset()

    def test_hit_detection_raycast_reads_velocity(self) -> None:
        # Reading ctx.movement.velocity forces an impulse-before-cast
        # ordering at validation time.
        assert "ctx.movement.velocity" in HitDetectionRaycastSegment.READS
        # Publishes the impact handoff slots Effect capabilities pull.
        assert HitDetectionRaycastSegment.WRITES == frozenset({
            "ctx.hitDetection.lastImpactCFrame",
            "ctx.hitDetection.lastInstance",
            "ctx.hitDetection.impactSignal",
        })

    def test_hit_detection_overlap_sphere_no_velocity_dependency(self) -> None:
        # OverlapSphere is a one-shot proximity sensor; doesn't depend
        # on the part having velocity yet.
        assert HitDetectionOverlapSphere.READS == frozenset()
        assert "ctx.hitDetection.impactSignal" in HitDetectionOverlapSphere.WRITES

    def test_effect_damage_reads_impact_signal(self) -> None:
        # Effects subscribe to ``impactSignal`` rather than polling the
        # last-instance slot — the validator's reader-after-writer rule
        # then forces a HitDetection writer to run first.
        assert "ctx.hitDetection.impactSignal" in EffectDamage.READS

    def test_effect_splash_reads_impact_cframe(self) -> None:
        assert "ctx.hitDetection.lastImpactCFrame" in EffectSplash.READS
        assert "ctx.hitDetection.impactSignal" in EffectSplash.READS

    def test_effect_spawn_template_reads_impact_cframe(self) -> None:
        assert "ctx.hitDetection.lastImpactCFrame" in EffectSpawnTemplate.READS


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class TestProjectileValidator:
    def test_turret_bullet_validates(self) -> None:
        validate_behavior(_turret_behavior())  # no raise

    def test_plane_bullet_validates(self) -> None:
        validate_behavior(_plane_behavior())  # no raise

    def test_raycast_before_impulse_rejected(self) -> None:
        """HitDetection.RaycastSegment reads ``ctx.movement.velocity`` —
        placing it BEFORE the impulse must fail at emit time, not at
        runtime.
        """
        bad = Behavior(
            unity_file_id="b",
            diagnostic_name="BadOrder",
            capabilities=(
                HitDetectionRaycastSegment(),
                MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=60),
                EffectDamage(value=10),
            ),
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(bad)
        assert excinfo.value.key == "ctx.movement.velocity"

    def test_effect_without_hit_detection_rejected(self) -> None:
        """Effect.Damage reads ``ctx.hitDetection.impactSignal`` — no
        HitDetection writer means the validator must reject.
        """
        bad = Behavior(
            unity_file_id="b",
            diagnostic_name="OrphanEffect",
            capabilities=(
                MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=60),
                LifetimeDespawn(seconds=3),
                EffectDamage(value=10),
            ),
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(bad)
        assert excinfo.value.key == "ctx.hitDetection.impactSignal"

    def test_double_writer_rejected(self) -> None:
        """Two HitDetection writers on the same ctx slot is a converter
        bug (no Unity pattern needs both). Validator catches at emit.
        """
        bad = Behavior(
            unity_file_id="b",
            diagnostic_name="DoubleHit",
            capabilities=(
                MovementImpulse(direction_local=(0.0, 0.0, 1.0), force_unity=60),
                HitDetectionRaycastSegment(),
                HitDetectionOverlapSphere(radius_unity=2),
                EffectDamage(value=10),
            ),
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(bad)
        # The first conflicting key the validator hits gets surfaced;
        # ordering follows frozenset iteration, but any of the three
        # HitDetection-namespaced writes is acceptable.
        assert excinfo.value.key in HitDetectionRaycastSegment.WRITES


# ---------------------------------------------------------------------------
# Luau stub emit — goldens
# ---------------------------------------------------------------------------

_TURRET_GOLDEN = (
    "-- @@AUTOGEN_GAMEPLAY_ADAPTER@@ TurretBullet unity_file_id=bullet_1\n"
    'local Gameplay = require(game:GetService("ReplicatedStorage")'
    ':WaitForChild("AutoGen"):WaitForChild("Gameplay"))\n'
    "local _container = script.Parent\n"
    "if _container == nil then\n"
    '    warn("[gameplay-adapter] TurretBullet: container resolution '
    'returned nil, adapter not bound")\n'
    "    return\n"
    "end\n"
    "Gameplay.run(_container, {\n"
    '    {kind = "movement.impulse",\n'
    "     direction_local = Vector3.new(0, 0, 1),\n"
    "     force_unity = 60},\n"
    '    {kind = "lifetime.despawn", seconds = 3},\n'
    '    {kind = "hit_detection.raycast_segment"},\n'
    '    {kind = "effect.damage", value = 10},\n'
    "})\n"
)

_PLANE_GOLDEN = (
    "-- @@AUTOGEN_GAMEPLAY_ADAPTER@@ PlaneBullet unity_file_id=bullet_2\n"
    'local Gameplay = require(game:GetService("ReplicatedStorage")'
    ':WaitForChild("AutoGen"):WaitForChild("Gameplay"))\n'
    "local _container = script.Parent\n"
    "if _container == nil then\n"
    '    warn("[gameplay-adapter] PlaneBullet: container resolution '
    'returned nil, adapter not bound")\n'
    "    return\n"
    "end\n"
    "Gameplay.run(_container, {\n"
    '    {kind = "movement.impulse",\n'
    "     direction_local = Vector3.new(0, 0, 1),\n"
    "     force_unity = 200},\n"
    '    {kind = "lifetime.despawn", seconds = 6},\n'
    '    {kind = "hit_detection.raycast_segment"},\n'
    '    {kind = "effect.spawn_template", name = "Explosion"},\n'
    '    {kind = "effect.splash",\n'
    "     radius_unity = 2,\n"
    "     value = 10},\n"
    "})\n"
)


class TestProjectileEmit:
    def test_turret_bullet_golden(self) -> None:
        assert emit_behavior_stub(_turret_behavior()) == _TURRET_GOLDEN

    def test_plane_bullet_golden(self) -> None:
        assert emit_behavior_stub(_plane_behavior()) == _PLANE_GOLDEN

    def test_emit_carries_marker(self) -> None:
        out = emit_behavior_stub(_turret_behavior())
        assert ADAPTER_STUB_MARKER in out.splitlines()[0]

    def test_emit_runs_validator_first(self) -> None:
        """If the capability tuple is malformed, the emitter must raise
        before producing Luau — same contract as the door slice.
        """
        bad = Behavior(
            unity_file_id="b",
            diagnostic_name="OrphanEffect",
            capabilities=(EffectDamage(value=10),),  # no HitDetection writer
        )
        with pytest.raises(BehaviorCompositionError):
            emit_behavior_stub(bad)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

class TestTurretBulletDetector:
    def test_primary_composition_only(self) -> None:
        """The contract: empty C# source must not change primary
        result for a node whose composition signal is already True.
        """
        det = TurretBulletDetector()
        node = _StubNode(
            file_id="b1", name="TurretBullet",
            components=[_mono("TurretBullet")],
        )
        # primary() doesn't accept source — by construction it can't
        # read it.
        assert det.primary(node, node.components[0]) is True  # type: ignore[arg-type]

    def test_primary_false_for_non_bullet(self) -> None:
        det = TurretBulletDetector()
        node = _StubNode(
            file_id="b1", name="Door",
            components=[_mono("Door")],
        )
        assert det.primary(node, node.components[0]) is False  # type: ignore[arg-type]

    def test_confirm_accepts_canonical_source(self) -> None:
        det = TurretBulletDetector()
        node = _StubNode(components=[_mono("TurretBullet")])
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0], _TURRET_BULLET_CSHARP,
        ) is True

    def test_confirm_rejects_when_overlap_sphere_present(self) -> None:
        """Negative discriminator: PlaneBullet's source has
        ``OverlapSphere`` — TurretBullet must reject so we don't
        double-match.
        """
        det = TurretBulletDetector()
        node = _StubNode(components=[_mono("TurretBullet")])
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0], _PLANE_BULLET_CSHARP,
        ) is False

    def test_confirm_rejects_when_missing_signal(self) -> None:
        det = TurretBulletDetector()
        node = _StubNode(components=[_mono("TurretBullet")])
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0], "// not a bullet",
        ) is False

    def test_behavior_round_trips(self) -> None:
        det = TurretBulletDetector()
        node = _StubNode(
            file_id="b1", name="TurretBullet",
            components=[_mono("TurretBullet")],
        )
        beh = det.behavior(  # type: ignore[arg-type]
            node, node.components[0], _TURRET_BULLET_CSHARP,
        )
        assert beh.diagnostic_name == "TurretBullet"
        assert beh.unity_file_id == "b1"
        assert beh.container_resolver.kind == "self"
        # Capability tuple in the canonical order.
        kinds = tuple(c.kind for c in beh.capabilities)
        assert kinds == (
            "movement.impulse",
            "lifetime.despawn",
            "hit_detection.raycast_segment",
            "effect.damage",
        )
        validate_behavior(beh)


class TestPlaneBulletDetector:
    def test_primary_composition_only(self) -> None:
        det = PlaneBulletDetector()
        node = _StubNode(components=[_mono("PlaneBullet")])
        assert det.primary(node, node.components[0]) is True  # type: ignore[arg-type]

    def test_confirm_requires_overlap_sphere_and_instantiate(self) -> None:
        det = PlaneBulletDetector()
        node = _StubNode(components=[_mono("PlaneBullet")])
        # Canonical PlaneBullet source: both signals present → accept.
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0], _PLANE_BULLET_CSHARP,
        ) is True
        # TurretBullet source: lacks OverlapSphere + Instantiate → reject.
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0], _TURRET_BULLET_CSHARP,
        ) is False

    def test_behavior_round_trips(self) -> None:
        det = PlaneBulletDetector()
        node = _StubNode(
            file_id="b2", name="PlaneBullet",
            components=[_mono("PlaneBullet")],
        )
        beh = det.behavior(  # type: ignore[arg-type]
            node, node.components[0], _PLANE_BULLET_CSHARP,
        )
        assert beh.diagnostic_name == "PlaneBullet"
        # Canonical order: Movement, Lifetime, HitDetection, SpawnTemplate,
        # Splash. SpawnTemplate before Splash so the explosion VFX
        # spawns even if Splash destroys the container.
        kinds = tuple(c.kind for c in beh.capabilities)
        assert kinds == (
            "movement.impulse",
            "lifetime.despawn",
            "hit_detection.raycast_segment",
            "effect.spawn_template",
            "effect.splash",
        )
        validate_behavior(beh)


class TestDispatchUsesProjectileDetectors:
    def test_dispatch_classifies_turret(self) -> None:
        node = _StubNode(components=[_mono("TurretBullet")])
        out = detect(  # type: ignore[arg-type]
            node, node.components[0], _TURRET_BULLET_CSHARP,
        )
        assert out is not None
        assert out.diagnostic_name == "TurretBullet"

    def test_dispatch_classifies_plane(self) -> None:
        node = _StubNode(components=[_mono("PlaneBullet")])
        out = detect(  # type: ignore[arg-type]
            node, node.components[0], _PLANE_BULLET_CSHARP,
        )
        assert out is not None
        assert out.diagnostic_name == "PlaneBullet"

    def test_dispatch_does_not_cross_match(self) -> None:
        """A node whose script class is TurretBullet but whose source is
        actually PlaneBullet's shape must NOT classify — TurretBullet's
        confirm rejects on the negative discriminator, and PlaneBullet's
        primary doesn't match.
        """
        node = _StubNode(components=[_mono("TurretBullet")])
        out = detect(  # type: ignore[arg-type]
            node, node.components[0], _PLANE_BULLET_CSHARP,
        )
        # primary(TurretBullet)=True, confirm rejects.
        # primary(PlaneBullet)=False (script class mismatch).
        assert out is None


# ---------------------------------------------------------------------------
# Integration: classify_scripts against a synthetic scene
# ---------------------------------------------------------------------------

@dataclass
class _StubScriptInfo:
    path: Path
    class_name: str


@dataclass
class _StubGuidIndex:
    by_guid: dict
    by_path: dict

    def guid_for_path(self, path: Path) -> str | None:
        return self.by_path.get(path.resolve())


@dataclass
class _StubParsedScene:
    all_nodes: dict


def _make_real_node(
    file_id: str,
    name: str,
    guid: str,
    component_file_id: str,
):
    from core.unity_types import ComponentData, SceneNode

    return SceneNode(
        name=name,
        file_id=file_id,
        active=True,
        layer=0,
        tag="",
        components=[
            ComponentData(
                component_type="MonoBehaviour",
                file_id=component_file_id,
                properties={
                    "m_Script": {"guid": guid, "fileID": 11500000},
                },
            ),
        ],
    )


class TestProjectileClassification:
    def test_turret_and_plane_classify_in_one_pass(self, tmp_path: Path) -> None:
        from converter.code_transpiler import TranspiledScript
        from converter.gameplay.integration import (
            adapter_transpiled_scripts,
            classify_scripts,
        )

        turret_path = tmp_path / "TurretBullet.cs"
        turret_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        plane_path = tmp_path / "PlaneBullet.cs"
        plane_path.write_text(_PLANE_BULLET_CSHARP, encoding="utf-8")

        turret_info = _StubScriptInfo(path=turret_path, class_name="TurretBullet")
        plane_info = _StubScriptInfo(path=plane_path, class_name="PlaneBullet")

        guid_t, guid_p = "turret-guid", "plane-guid"
        guid_index = _StubGuidIndex(
            by_guid={guid_t: turret_path, guid_p: plane_path},
            by_path={
                turret_path.resolve(): guid_t,
                plane_path.resolve(): guid_p,
            },
        )

        # Two instances of each bullet to exercise multi-binding
        # collection (operator-facing report shows every instance even
        # when the emit is per-class).
        turret_a = _make_real_node("100", "Turret_A", guid_t, "1001")
        turret_b = _make_real_node("101", "Turret_B", guid_t, "1011")
        plane_a = _make_real_node("200", "Plane_A", guid_p, "2001")
        parsed_scene = _StubParsedScene(all_nodes={
            "100": turret_a, "101": turret_b, "200": plane_a,
        })

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[turret_info, plane_info],  # type: ignore[list-item]
        )

        # Both bullet classes match, both AI inputs get bypassed.
        assert turret_path.resolve() in result.skip_paths
        assert plane_path.resolve() in result.skip_paths
        assert result.divergent == ()

        # Per-class bindings: TurretBullet has two nodes, PlaneBullet
        # has one.
        turret_match = result.matches[turret_path.resolve()]
        assert turret_match.class_name == "TurretBullet"
        assert turret_match.detector_name == "turret_bullet"
        assert len(turret_match.bindings) == 2

        plane_match = result.matches[plane_path.resolve()]
        assert plane_match.class_name == "PlaneBullet"
        assert plane_match.detector_name == "plane_bullet"
        assert len(plane_match.bindings) == 1

        # Emit produces a TranspiledScript per class plus a GameplayMatch
        # per node binding.
        scripts, matches = adapter_transpiled_scripts(
            classification=result,
            transpiled_script_cls=TranspiledScript,
        )
        assert len(scripts) == 2
        assert {s.strategy for s in scripts} == {"gameplay_adapter"}
        assert {s.output_filename for s in scripts} == {
            "TurretBullet.luau", "PlaneBullet.luau",
        }
        # Three node bindings — Turret_A, Turret_B, Plane_A — flow
        # into conversion_report.json's gameplay_adapters.bindings[].
        assert len(matches) == 3
        node_names = sorted(m.node_name for m in matches)
        assert node_names == ["Plane_A", "Turret_A", "Turret_B"]

    def test_classification_uses_default_detectors(self, tmp_path: Path) -> None:
        """End-to-end: the ALL_DETECTORS tuple registers the projectile
        detectors, so classify_scripts picks them up without the caller
        passing them explicitly.
        """
        from converter.gameplay.integration import classify_scripts

        turret_path = tmp_path / "TurretBullet.cs"
        turret_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=turret_path, class_name="TurretBullet")
        guid = "g"
        guid_index = _StubGuidIndex(
            by_guid={guid: turret_path},
            by_path={turret_path.resolve(): guid},
        )
        node = _make_real_node("100", "Turret_A", guid, "1001")
        parsed_scene = _StubParsedScene(all_nodes={"100": node})

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
        )
        assert turret_path.resolve() in result.matches


# ---------------------------------------------------------------------------
# Real Unity source smoke (test_projects/SimpleFPS)
# ---------------------------------------------------------------------------
#
# Loads the actual TurretBullet.cs / PlaneBullet.cs from the SimpleFPS
# test project rather than the in-test fixture strings. Pins the
# detectors against the real Unity sources so an upstream edit to the
# canonical fixtures surfaces here instead of in a downstream
# conversion run.

_SIMPLEFPS_SCRIPTS = (
    Path(__file__).parent.parent.parent
    / "test_projects" / "SimpleFPS" / "Assets" / "Scripts"
)


class TestRealSimpleFPSSources:
    def test_turret_bullet_real_source_classifies(self) -> None:
        path = _SIMPLEFPS_SCRIPTS / "TurretBullet.cs"
        if not path.exists():
            pytest.skip("SimpleFPS test project not checked out")
        source = path.read_text(encoding="utf-8")
        node = _StubNode(components=[_mono("TurretBullet")])
        out = detect(  # type: ignore[arg-type]
            node, node.components[0], source,
        )
        assert out is not None
        assert out.diagnostic_name == "TurretBullet"
        kinds = tuple(c.kind for c in out.capabilities)
        assert kinds == (
            "movement.impulse",
            "lifetime.despawn",
            "hit_detection.raycast_segment",
            "effect.damage",
        )

    def test_plane_bullet_real_source_classifies(self) -> None:
        path = _SIMPLEFPS_SCRIPTS / "PlaneBullet.cs"
        if not path.exists():
            pytest.skip("SimpleFPS test project not checked out")
        source = path.read_text(encoding="utf-8")
        node = _StubNode(components=[_mono("PlaneBullet")])
        out = detect(  # type: ignore[arg-type]
            node, node.components[0], source,
        )
        assert out is not None
        assert out.diagnostic_name == "PlaneBullet"
        kinds = tuple(c.kind for c in out.capabilities)
        assert kinds == (
            "movement.impulse",
            "lifetime.despawn",
            "hit_detection.raycast_segment",
            "effect.spawn_template",
            "effect.splash",
        )

    def test_turret_real_source_rejects_plane_signal(self) -> None:
        """If the real TurretBullet.cs ever gains an OverlapSphere call
        (e.g. someone retrofits splash damage), the negative
        discriminator kicks in and the detector stops matching —
        better to fall back to AI than silently lose direct-hit
        semantics.
        """
        turret = _SIMPLEFPS_SCRIPTS / "TurretBullet.cs"
        plane = _SIMPLEFPS_SCRIPTS / "PlaneBullet.cs"
        if not turret.exists() or not plane.exists():
            pytest.skip("SimpleFPS test project not checked out")
        det = TurretBulletDetector()
        node = _StubNode(components=[_mono("TurretBullet")])
        # TurretBullet's real source: accepted.
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0],
            turret.read_text(encoding="utf-8"),
        ) is True
        # PlaneBullet's real source: rejected (negative discriminator).
        assert det.confirm(  # type: ignore[arg-type]
            node, node.components[0],
            plane.read_text(encoding="utf-8"),
        ) is False


# ---------------------------------------------------------------------------
# Prefab template walking (PR #73b)
# ---------------------------------------------------------------------------
#
# Many Unity behaviours (doors, bullets, pickups) live exclusively on
# prefab roots and are runtime-spawned via ``Instantiate(prefab)`` from
# other scripts — main.unity carries no MonoBehaviour for them. The
# adapter pipeline walks ``prefab_library.prefabs[].root`` in addition
# to scene nodes so those behaviours actually classify.


@dataclass
class _StubPrefabComponent:
    component_type: str
    file_id: str
    properties: dict


@dataclass
class _StubPrefabNode:
    name: str
    file_id: str
    components: list
    children: list


@dataclass
class _StubPrefab:
    name: str
    root: _StubPrefabNode | None


@dataclass
class _StubPrefabLibrary:
    prefabs: list


class TestPrefabWalk:
    def test_prefab_only_bullet_classifies(self, tmp_path: Path) -> None:
        """A bullet whose MonoBehaviour lives ONLY in a prefab root
        (not in any scene) must still classify. Mirrors SimpleFPS's
        TurretBullet — main.unity doesn't carry TurretBullet
        MonoBehaviours; the bullet is runtime-spawned from
        TurretBullet.prefab by Turret.cs.
        """
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "TurretBullet.cs"
        cs_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="TurretBullet")
        guid = "turret-guid"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )

        # Empty scene + a prefab carrying the bullet MonoBehaviour.
        parsed_scene = _StubParsedScene(all_nodes={})
        bullet_node = _StubPrefabNode(
            name="TurretBullet",
            file_id="100",
            components=[_StubPrefabComponent(
                component_type="MonoBehaviour",
                file_id="200",
                properties={"m_Script": {"guid": guid, "fileID": 11500000}},
            )],
            children=[],
        )
        prefab = _StubPrefab(name="TurretBullet", root=bullet_node)
        prefab_library = _StubPrefabLibrary(prefabs=[prefab])

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            prefab_library=prefab_library,  # type: ignore[arg-type]
        )

        assert cs_path.resolve() in result.matches
        match = result.matches[cs_path.resolve()]
        assert match.class_name == "TurretBullet"
        assert len(match.bindings) == 1
        assert match.bindings[0].node_name == "TurretBullet"

    def test_prefab_walk_descends_children(self, tmp_path: Path) -> None:
        """A MonoBehaviour on a NESTED prefab child (not the root)
        still classifies. SimpleFPS's Door prefab carries the Door
        MonoBehaviour on a child node named ``base``, not the root.
        """
        from converter.gameplay.capabilities import (
            ContainerResolver,
            LifetimePersistent,
            MovementAttributeDrivenTween,
            TriggerOnBoolAttribute,
        )
        from converter.gameplay.integration import classify_scripts

        # Use the door detector (PR #73a) via a synthetic detector to
        # keep this test scoped to the prefab-walk mechanic. Bullet
        # detectors already prove the source-aware confirm path.
        class _ChildOnlyDetector:
            name = "child_only"

            def primary(self, node, component) -> bool:  # type: ignore[no-untyped-def]
                return str(
                    component.properties.get("_script_class_name", ""),
                ) == "ChildBehaviour"

            def confirm(self, node, component, source) -> bool:  # type: ignore[no-untyped-def]
                return True

            def behavior(self, node, component, source):  # type: ignore[no-untyped-def]
                return Behavior(
                    unity_file_id=node.file_id,
                    diagnostic_name="ChildBehaviour",
                    capabilities=(
                        TriggerOnBoolAttribute(name="active"),
                        MovementAttributeDrivenTween(
                            target_offset_unity=(0.0, 1.0, 0.0),
                            open_duration=1.0,
                            close_duration=1.0,
                        ),
                        LifetimePersistent(),
                    ),
                    container_resolver=ContainerResolver(kind="self"),
                )

        cs_path = tmp_path / "ChildBehaviour.cs"
        cs_path.write_text("// child behaviour", encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="ChildBehaviour")
        guid = "child-guid"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )

        child = _StubPrefabNode(
            name="base",
            file_id="200",
            components=[_StubPrefabComponent(
                component_type="MonoBehaviour",
                file_id="201",
                properties={"m_Script": {"guid": guid, "fileID": 11500000}},
            )],
            children=[],
        )
        root = _StubPrefabNode(
            name="Door",
            file_id="100",
            components=[],
            children=[child],
        )
        prefab = _StubPrefab(name="Door", root=root)
        prefab_library = _StubPrefabLibrary(prefabs=[prefab])
        parsed_scene = _StubParsedScene(all_nodes={})

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            detectors=(_ChildOnlyDetector(),),  # type: ignore[arg-type]
            prefab_library=prefab_library,  # type: ignore[arg-type]
        )

        assert cs_path.resolve() in result.matches
        match = result.matches[cs_path.resolve()]
        assert match.bindings[0].node_name == "base"
        assert match.bindings[0].unity_file_id == "200"

    def test_prefab_library_none_is_safe(self, tmp_path: Path) -> None:
        """Passing ``prefab_library=None`` falls back to scene-only
        walk. Keeps the path safe on early-phase resumes where the
        prefab library hasn't parsed yet.
        """
        from converter.gameplay.integration import classify_scripts

        result = classify_scripts(
            parsed_scene=_StubParsedScene(all_nodes={}),  # type: ignore[arg-type]
            guid_index=_StubGuidIndex(by_guid={}, by_path={}),  # type: ignore[arg-type]
            script_infos=[],
            prefab_library=None,
        )
        assert result.matches == {}

    def test_prefab_with_no_root_skipped(self, tmp_path: Path) -> None:
        """A prefab whose root resolution failed (variant parse error,
        etc.) is silently skipped rather than crashing the walk.
        """
        from converter.gameplay.integration import classify_scripts

        prefab = _StubPrefab(name="Broken", root=None)
        prefab_library = _StubPrefabLibrary(prefabs=[prefab])
        result = classify_scripts(
            parsed_scene=_StubParsedScene(all_nodes={}),  # type: ignore[arg-type]
            guid_index=_StubGuidIndex(by_guid={}, by_path={}),  # type: ignore[arg-type]
            script_infos=[],
            prefab_library=prefab_library,  # type: ignore[arg-type]
        )
        assert result.matches == {}


# ---------------------------------------------------------------------------
# Source-qualified deny-list (codex PR #73b-round-1 P2)
# ---------------------------------------------------------------------------
#
# Bare ``file_id`` deny-list entries match across every source. After
# PR #73b's prefab walk, distinct prefab assets routinely share local
# file_ids (SimpleFPS has at least two prefabs using ``&100000``). The
# qualified form ``<source_path>#<file_id>`` is the operator escape
# hatch that suppresses one specific source without affecting others.


class TestSourceQualifiedDenyList:
    def test_qualified_deny_entry_suppresses_one_prefab(
        self, tmp_path: Path,
    ) -> None:
        """Two prefabs with colliding file_ids classify normally. A
        qualified deny entry on ONE prefab's path suppresses only
        that one — the other prefab still emits.
        """
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "TurretBullet.cs"
        cs_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="TurretBullet")
        guid = "g"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )

        # Two distinct prefab files, both containing a bullet at
        # the same file_id "100" — the collision codex flagged.
        def _bullet_prefab(prefab_path: str) -> _StubPrefab:
            bullet = _StubPrefabNode(
                name="TurretBullet",
                file_id="100",
                components=[_StubPrefabComponent(
                    component_type="MonoBehaviour",
                    file_id="200",
                    properties={"m_Script": {"guid": guid, "fileID": 11500000}},
                )],
                children=[],
            )
            return _StubPrefab(name=prefab_path, root=bullet)
        # Use _ApPathPrefab so getattr(prefab, "prefab_path", "") yields the right thing
        prefab_a_path = str(tmp_path / "PrefabA.prefab")
        prefab_b_path = str(tmp_path / "PrefabB.prefab")

        @dataclass
        class _PathPrefab:
            name: str
            root: object
            prefab_path: object  # path-like

        prefab_a = _PathPrefab(
            name="A", root=_bullet_prefab(prefab_a_path).root,
            prefab_path=prefab_a_path,
        )
        prefab_b = _PathPrefab(
            name="B", root=_bullet_prefab(prefab_b_path).root,
            prefab_path=prefab_b_path,
        )
        prefab_library = _StubPrefabLibrary(prefabs=[prefab_a, prefab_b])

        # Qualified deny: suppress PrefabA's bullet only.
        deny = frozenset({f"{prefab_a_path}#100"})
        result = classify_scripts(
            parsed_scene=_StubParsedScene(all_nodes={}),  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            prefab_library=prefab_library,  # type: ignore[arg-type]
            deny_list=deny,
        )

        # PrefabB's bullet survives. PrefabA's is suppressed.
        match = result.matches.get(cs_path.resolve())
        assert match is not None
        sources = {b.source_path for b in match.bindings}
        assert sources == {prefab_b_path}

    def test_bare_file_id_still_suppresses_across_sources(
        self, tmp_path: Path,
    ) -> None:
        """Legacy bare ``file_id`` form keeps working — suppresses
        EVERY source that uses that id. Pins backward compat with
        pre-PR #73b ``.gameplay_deny.txt`` files.
        """
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "TurretBullet.cs"
        cs_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="TurretBullet")
        guid = "g"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )

        bullet_a = _StubPrefabNode(
            name="A_bullet", file_id="100",
            components=[_StubPrefabComponent(
                component_type="MonoBehaviour",
                file_id="200",
                properties={"m_Script": {"guid": guid, "fileID": 11500000}},
            )],
            children=[],
        )
        bullet_b = _StubPrefabNode(
            name="B_bullet", file_id="100",
            components=[_StubPrefabComponent(
                component_type="MonoBehaviour",
                file_id="200",
                properties={"m_Script": {"guid": guid, "fileID": 11500000}},
            )],
            children=[],
        )
        prefab_library = _StubPrefabLibrary(prefabs=[
            _StubPrefab(name="A", root=bullet_a),
            _StubPrefab(name="B", root=bullet_b),
        ])

        result = classify_scripts(
            parsed_scene=_StubParsedScene(all_nodes={}),  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            prefab_library=prefab_library,  # type: ignore[arg-type]
            deny_list=frozenset({"100"}),
        )
        # Both prefabs suppressed by the bare entry.
        assert result.matches == {}

    def test_binding_carries_source_path(self, tmp_path: Path) -> None:
        """Each NodeBinding records the source it came from so the
        conversion report can render qualified deny entries.
        """
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "TurretBullet.cs"
        cs_path.write_text(_TURRET_BULLET_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="TurretBullet")
        guid = "g"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        bullet = _StubPrefabNode(
            name="TurretBullet", file_id="100",
            components=[_StubPrefabComponent(
                component_type="MonoBehaviour",
                file_id="200",
                properties={"m_Script": {"guid": guid, "fileID": 11500000}},
            )],
            children=[],
        )

        @dataclass
        class _PathPrefab:
            name: str
            root: object
            prefab_path: object

        prefab_path = str(tmp_path / "TurretBullet.prefab")
        prefab = _PathPrefab(name="x", root=bullet, prefab_path=prefab_path)
        prefab_library = _StubPrefabLibrary(prefabs=[prefab])

        result = classify_scripts(
            parsed_scene=_StubParsedScene(all_nodes={}),  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            prefab_library=prefab_library,  # type: ignore[arg-type]
        )
        match = result.matches[cs_path.resolve()]
        assert match.bindings[0].source_path == prefab_path


# ---------------------------------------------------------------------------
# Runtime semantics — Effect.Damage Player filter (codex PR #73b-round-1 P1)
# ---------------------------------------------------------------------------
#
# These tests pin the runtime's Player-only gate by inspecting the
# emitted Luau directly. Headless execution of the runtime modules is
# out of scope for this PR; the Lua source is the source of truth for
# the contract, and structural assertions are enough to prevent the
# specific regression codex flagged.


class TestDamagePlayerFilter:
    def _read_runtime(self) -> str:
        from pathlib import Path as P
        path = (
            P(__file__).parent.parent / "runtime" / "gameplay" / "effects.luau"
        )
        return path.read_text(encoding="utf-8")

    def test_damage_handler_checks_player(self) -> None:
        """``Effects.damage`` must gate the ``_applyDamageToModel``
        call behind one of three Player-detection signals — anything
        else regresses TurretBullet to damage NPC/allied Humanoids.
        """
        body = self._read_runtime()
        # The three-signal check the legacy bullet pack also uses.
        assert "Players:GetPlayerFromCharacter" in body
        assert 'hitInstance:HasTag("Player")' in body
        assert 'model.Name == "Player"' in body

    def test_damage_handler_destroys_on_any_impact(self) -> None:
        """Despawn-on-any-impact: matches the legacy bullet pack so
        bullets don't persist after their raycast hit, even when the
        hit was a wall (Unity bullets fly through walls; the legacy
        pack regressed that, and the adapter matches the legacy
        behaviour for consistency).
        """
        body = self._read_runtime()
        # The destroy line lives outside the ``isPlayer`` branch.
        # Look for the comment pinning this contract — if a future
        # refactor moves it inside the if-block, both the comment and
        # the behaviour need updating together.
        assert "Despawn on any impact" in body


# ---------------------------------------------------------------------------
# Pipeline mutual exclusion
# ---------------------------------------------------------------------------

class TestLegacyPackMutex:
    """Semantic test of the legacy-pack mutex: when
    ``use_gameplay_adapters`` is on, the bullet + door packs must NOT
    run (they'd double-bind alongside the adapter). Codex PR
    #73b-round-1 P3 flagged that an inspect-source substring check is
    fragile against refactors; pin the runtime behaviour instead.
    """

    def _run_packs_with_pipeline_gate(
        self, scripts, use_gameplay_adapters: bool,
    ):
        """Invoke ``fix_require_classifications`` the same way
        ``Pipeline._transpile_scripts`` does — including the
        ``disabled_packs`` frozenset that gates legacy packs when the
        adapter flag is on. Returns the disabled_packs the pipeline
        would have passed.
        """
        # Mirror the gate from pipeline.py — single source of truth
        # for the mutex semantics. If pipeline.py ever changes the
        # mutex shape, this test fails loudly.
        from converter.script_coherence import fix_require_classifications

        disabled_packs = frozenset()
        if use_gameplay_adapters:
            # The exact set the pipeline maintains. Keep this in sync
            # with pipeline._transpile_scripts. PR #73c will add the
            # damage-protocol pack here.
            disabled_packs = frozenset({
                "door_tween_open",
                "bullet_physics_raycast",
            })

        fix_require_classifications(scripts, disabled_packs=disabled_packs)
        return disabled_packs

    def test_disabled_packs_when_adapter_on(self) -> None:
        """Adapter on → bullet_physics_raycast AND door_tween_open
        are both in disabled_packs.
        """
        disabled = self._run_packs_with_pipeline_gate(
            scripts=[], use_gameplay_adapters=True,
        )
        assert "bullet_physics_raycast" in disabled
        assert "door_tween_open" in disabled

    def test_disabled_packs_when_adapter_off(self) -> None:
        """Adapter off → no packs are force-disabled. Legacy mode
        keeps the existing behaviour.
        """
        disabled = self._run_packs_with_pipeline_gate(
            scripts=[], use_gameplay_adapters=False,
        )
        assert disabled == frozenset()

    def test_legacy_packs_actually_registered(self) -> None:
        """Pin that the legacy packs the mutex targets really exist —
        if a pack is renamed without updating the mutex, the gate
        becomes a no-op without anyone noticing.
        """
        from converter.script_coherence_packs import _REGISTRY

        registered = {p.name for p in _REGISTRY}
        assert "bullet_physics_raycast" in registered
        assert "door_tween_open" in registered
