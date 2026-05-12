"""Tests for the gameplay-adapter framework (PR #73a).

Covers:
  - Capability dataclasses (immutability, declared READS/WRITES).
  - Emit-time validator: empty capabilities, namespace check,
    reader-after-writer, single-writer-per-key, happy path.
  - Luau stub emitter — both ContainerResolver variants render the
    expected Lua, and the validator runs before the emit.
  - Detector contract: ``primary()`` must NOT depend on C# source
    content (composition-only).
  - Multi-detector resolution: AmbiguousDetectionError on two-match;
    deny-list suppresses everything for a given file_id.
  - End-to-end ``run_gameplay_adapters`` against a synthetic ParsedScene
    that mimics a SciFi_Door binding.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from converter.gameplay.capabilities import (
    Behavior,
    ContainerResolver,
    CTX_FAMILIES,
    LifetimePersistent,
    MovementAttributeDrivenTween,
    TriggerOnBoolAttribute,
)
from converter.gameplay.composer import (
    BehaviorCompositionError,
    emit_behavior_stub,
    validate_behavior,
)
from converter.gameplay.detectors import (
    ALL_DETECTORS,
    AmbiguousDetectionError,
    DENY_LIST_FILENAME,
    Detector,
    DoorDetector,
    detect,
    load_deny_list,
)


# ---------------------------------------------------------------------------
# Capability dataclasses
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_trigger_on_bool_attribute_is_frozen(self) -> None:
        cap = TriggerOnBoolAttribute(name="open")
        with pytest.raises(Exception):
            cap.name = "closed"  # type: ignore[misc]

    def test_declared_reads_writes(self) -> None:
        # Trigger writes only the trigger.value + trigger.changed slots
        assert TriggerOnBoolAttribute.WRITES == frozenset({
            "ctx.trigger.value",
            "ctx.trigger.changed",
        })
        assert TriggerOnBoolAttribute.READS == frozenset()
        assert MovementAttributeDrivenTween.READS == frozenset({
            "ctx.trigger.value",
            "ctx.trigger.changed",
        })
        assert MovementAttributeDrivenTween.WRITES == frozenset()
        assert LifetimePersistent.READS == frozenset()
        assert LifetimePersistent.WRITES == frozenset()

    def test_kinds_are_unique(self) -> None:
        kinds = {
            TriggerOnBoolAttribute.kind,
            MovementAttributeDrivenTween.kind,
            LifetimePersistent.kind,
        }
        assert len(kinds) == 3


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _door_behavior(
    resolver: ContainerResolver | None = None,
) -> Behavior:
    return Behavior(
        unity_file_id="42",
        diagnostic_name="Door",
        capabilities=(
            TriggerOnBoolAttribute(name="open"),
            MovementAttributeDrivenTween(
                target_offset_unity=(0.0, 4.0, 0.0),
                open_duration=1.0,
                close_duration=1.0,
            ),
            LifetimePersistent(),
        ),
        container_resolver=resolver or ContainerResolver(
            kind="ascend_then_child", child_name="door",
        ),
    )


class TestValidator:
    def test_happy_path_door(self) -> None:
        validate_behavior(_door_behavior())  # no raise

    def test_empty_capabilities_raises(self) -> None:
        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Empty",
            capabilities=(),
        )
        with pytest.raises(BehaviorCompositionError):
            validate_behavior(b)

    def test_reader_before_writer_raises(self) -> None:
        # Movement reads ctx.trigger.value but trigger never appears.
        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Door",
            capabilities=(
                MovementAttributeDrivenTween(
                    target_offset_unity=(0.0, 4.0, 0.0),
                    open_duration=1.0,
                    close_duration=1.0,
                ),
            ),
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(b)
        assert "ctx.trigger" in str(excinfo.value)
        assert excinfo.value.capability_index == 0

    def test_reader_after_writer_swapped_raises(self) -> None:
        # Movement (reader) appears BEFORE Trigger (writer).
        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Door",
            capabilities=(
                MovementAttributeDrivenTween(
                    target_offset_unity=(0.0, 4.0, 0.0),
                    open_duration=1.0,
                    close_duration=1.0,
                ),
                TriggerOnBoolAttribute(name="open"),
            ),
        )
        with pytest.raises(BehaviorCompositionError):
            validate_behavior(b)

    def test_double_writer_raises(self) -> None:
        # Two TriggerOnBoolAttribute caps would both write
        # ctx.trigger.value — single-writer violation.
        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Door",
            capabilities=(
                TriggerOnBoolAttribute(name="open"),
                TriggerOnBoolAttribute(name="locked"),
            ),
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(b)
        assert excinfo.value.capability_index == 1
        assert excinfo.value.key in TriggerOnBoolAttribute.WRITES

    def test_namespace_check(self) -> None:
        # Fabricate a capability with an unknown family.
        @dataclass(frozen=True)
        class BadCap:
            kind: ClassVar[str] = "weird.cap"
            READS: ClassVar[frozenset[str]] = frozenset()
            WRITES: ClassVar[frozenset[str]] = frozenset({"ctx.unknown.x"})

        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Bad",
            capabilities=(BadCap(),),  # type: ignore[arg-type]
        )
        with pytest.raises(BehaviorCompositionError) as excinfo:
            validate_behavior(b)
        assert "unknown" in str(excinfo.value)
        assert "family" in str(excinfo.value)

    def test_all_known_families_recognized(self) -> None:
        # Validator's family allowlist matches CTX_FAMILIES — if a
        # contributor adds a family to one without updating the other,
        # this test fails loudly.
        assert "trigger" in CTX_FAMILIES
        assert "movement" in CTX_FAMILIES
        assert "lifetime" in CTX_FAMILIES
        assert "hitDetection" in CTX_FAMILIES
        assert "effect" in CTX_FAMILIES


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

class TestEmitter:
    def test_emit_door_stub(self) -> None:
        out = emit_behavior_stub(_door_behavior())
        # Header carries the unity_file_id so deny-list lookups are
        # operator-discoverable.
        assert "unity_file_id=42" in out
        assert "diagnostic_name" not in out  # comment formatting check
        assert 'WaitForChild("Gameplay")' in out
        # Container resolver: ascend_then_child uses BOUNDED
        # WaitForChild + warn-on-nil path (codex P2 fix). The
        # emitted IIFE expression contains the bounded wait literal.
        assert 'script.Parent.Parent or script.Parent' in out
        assert ':WaitForChild("door", 5)' in out
        # Call site guards against nil-container before dispatch.
        assert "if _container == nil then" in out
        # Capabilities appear in tuple order
        idx_trigger = out.find("trigger.on_bool_attribute")
        idx_movement = out.find("movement.attribute_driven_tween")
        idx_lifetime = out.find("lifetime.persistent")
        assert 0 < idx_trigger < idx_movement < idx_lifetime

    def test_emit_self_container(self) -> None:
        b = _door_behavior(resolver=ContainerResolver(kind="self"))
        out = emit_behavior_stub(b)
        assert "local _container = script.Parent\n" in out
        # No sibling lookup in the container resolution line itself —
        # the require above uses WaitForChild for the AutoGen Folder,
        # so we narrow the assertion to the container line only.
        container_line = next(
            line for line in out.splitlines()
            if line.startswith("local _container")
        )
        assert "WaitForChild" not in container_line
        assert 'require(game:GetService("ReplicatedStorage")' in out

    def test_emit_validates_first(self) -> None:
        # Empty capabilities — emit should raise BEFORE rendering.
        b = Behavior(
            unity_file_id="x", diagnostic_name="Bad", capabilities=(),
        )
        with pytest.raises(BehaviorCompositionError):
            emit_behavior_stub(b)

    def test_ascend_then_child_requires_name(self) -> None:
        b = Behavior(
            unity_file_id="x",
            diagnostic_name="Door",
            capabilities=(
                TriggerOnBoolAttribute(name="open"),
                LifetimePersistent(),
            ),
            container_resolver=ContainerResolver(
                kind="ascend_then_child", child_name="",
            ),
        )
        with pytest.raises(BehaviorCompositionError):
            emit_behavior_stub(b)

    def test_escapes_quotes_in_names(self) -> None:
        # Defensive: an attribute name with a quote shouldn't break
        # the emitted Lua string literal.
        b = _door_behavior()
        b = Behavior(
            unity_file_id=b.unity_file_id,
            diagnostic_name=b.diagnostic_name,
            capabilities=(
                TriggerOnBoolAttribute(name='wei"rd'),
                MovementAttributeDrivenTween(
                    target_offset_unity=(0.0, 4.0, 0.0),
                    open_duration=1.0,
                    close_duration=1.0,
                ),
                LifetimePersistent(),
            ),
            container_resolver=b.container_resolver,
        )
        out = emit_behavior_stub(b)
        assert r'"wei\"rd"' in out


# ---------------------------------------------------------------------------
# Detector contract
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


def _door_component(file_id: str = "c1") -> _StubComponent:
    return _StubComponent(
        component_type="MonoBehaviour",
        file_id=file_id,
        properties={"_script_class_name": "Door"},
    )


def _non_door_component(file_id: str = "c2") -> _StubComponent:
    return _StubComponent(
        component_type="MonoBehaviour",
        file_id=file_id,
        properties={"_script_class_name": "NotDoor"},
    )


def _node_with_door_script() -> _StubNode:
    return _StubNode(
        file_id="100", name="DoorTrigger",
        components=[_door_component()],
    )


def _node_without_door_script() -> _StubNode:
    return _StubNode(
        file_id="101", name="OtherNode",
        components=[_non_door_component()],
    )


_DOOR_CSHARP = """
public class Door : MonoBehaviour {
    private void OnTriggerEnter(Collider other) {
        if (other.tag == "Player") {
            doorAnim.SetBool("open", true);
        }
    }
}
"""


class TestDetectorContract:
    def test_primary_does_not_inspect_source(self) -> None:
        """The contract: ``primary()`` must classify on Unity composition
        only. Empty C# source must not change the primary result for a
        node whose composition signal is already True.
        """
        det = DoorDetector()
        node = _node_with_door_script()
        comp = node.components[0]
        # primary() doesn't take source at all — it can't read it by
        # construction. Component is passed alongside the node so a
        # second MonoBehaviour on the same node can't sneak through.
        assert det.primary(node, comp) is True  # type: ignore[arg-type]

    def test_primary_false_for_non_door_component(self) -> None:
        det = DoorDetector()
        node = _node_without_door_script()
        comp = node.components[0]
        assert det.primary(node, comp) is False  # type: ignore[arg-type]

    def test_primary_targets_component_not_whole_node(self) -> None:
        """A node carrying BOTH Door AND NotDoor must primary-match
        only when the Door component is the one being asked about.
        Pins the codex P1 fix.
        """
        det = DoorDetector()
        node = _StubNode(
            file_id="200", name="MixedNode",
            components=[_door_component("d"), _non_door_component("n")],
        )
        assert det.primary(node, node.components[0]) is True  # type: ignore[arg-type]
        assert det.primary(node, node.components[1]) is False  # type: ignore[arg-type]

    def test_confirm_rejects_when_source_missing_signal(self) -> None:
        det = DoorDetector()
        node = _node_with_door_script()
        comp = node.components[0]
        assert det.confirm(node, comp, "// not a door") is False  # type: ignore[arg-type]

    def test_confirm_accepts_canonical_door(self) -> None:
        det = DoorDetector()
        node = _node_with_door_script()
        comp = node.components[0]
        assert det.confirm(node, comp, _DOOR_CSHARP) is True  # type: ignore[arg-type]

    def test_behavior_round_trips(self) -> None:
        det = DoorDetector()
        node = _node_with_door_script()
        comp = node.components[0]
        beh = det.behavior(node, comp, _DOOR_CSHARP)  # type: ignore[arg-type]
        assert beh.diagnostic_name == "Door"
        assert beh.unity_file_id == "100"
        assert beh.container_resolver.kind == "ascend_then_child"
        assert beh.container_resolver.child_name == "door"
        validate_behavior(beh)


# ---------------------------------------------------------------------------
# Multi-detector dispatch / deny-list
# ---------------------------------------------------------------------------

class _AlwaysMatch:
    """Synthetic detector that primary+confirm always pass — useful
    for forcing AmbiguousDetectionError without inventing fake Unity
    fixtures.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def primary(self, node, component) -> bool:
        return True

    def confirm(self, node, component, source) -> bool:
        return True

    def behavior(self, node, component, source):
        return _door_behavior()


class TestDispatch:
    def test_no_match_returns_none(self) -> None:
        node = _node_without_door_script()
        out = detect(node, node.components[0], "")  # type: ignore[arg-type]
        assert out is None

    def test_single_match_returns_behavior(self) -> None:
        node = _node_with_door_script()
        out = detect(node, node.components[0], _DOOR_CSHARP)  # type: ignore[arg-type]
        assert out is not None
        assert out.diagnostic_name == "Door"

    def test_two_matches_raise_ambiguous(self) -> None:
        node = _node_with_door_script()
        dets = (_AlwaysMatch("alpha"), _AlwaysMatch("beta"))
        with pytest.raises(AmbiguousDetectionError) as excinfo:
            detect(
                node, node.components[0], _DOOR_CSHARP, detectors=dets,  # type: ignore[arg-type]
            )
        assert sorted(excinfo.value.detector_names) == ["alpha", "beta"]
        assert excinfo.value.component_file_id == "c1"

    def test_deny_list_suppresses_match_by_node_id(self) -> None:
        node = _node_with_door_script()
        out = detect(
            node, node.components[0], _DOOR_CSHARP,  # type: ignore[arg-type]
            deny_list=frozenset({"100"}),
        )
        assert out is None

    def test_deny_list_suppresses_match_by_component_id(self) -> None:
        node = _node_with_door_script()
        out = detect(
            node, node.components[0], _DOOR_CSHARP,  # type: ignore[arg-type]
            deny_list=frozenset({"c1"}),
        )
        assert out is None

    def test_load_deny_list_strips_comments_and_blanks(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / DENY_LIST_FILENAME
        path.write_text(
            "# header comment\n"
            "\n"
            "100\n"
            "# inline comment\n"
            "  200  \n",
            encoding="utf-8",
        )
        out = load_deny_list(str(tmp_path))
        assert out == frozenset({"100", "200"})

    def test_load_deny_list_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_deny_list(str(tmp_path)) == frozenset()


# ---------------------------------------------------------------------------
# End-to-end integration
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


@dataclass
class _StubTranspiledScript:
    source_path: str
    luau_source: str = ""
    output_filename: str = ""
    csharp_source: str = ""
    strategy: str = "ai"
    confidence: float = 0.5
    flagged_for_review: bool = False
    warnings: list = None  # type: ignore[assignment]
    script_type: str = "Script"

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def _make_real_node(
    file_id: str, name: str, guid: str, component_file_id: str = "501",
):
    """Build a real SceneNode with a MonoBehaviour pointing at *guid*."""
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


class TestClassifyAndEmit:
    def test_classify_then_emit_replaces_door(self, tmp_path: Path) -> None:
        from converter.code_transpiler import TranspiledScript
        from converter.gameplay.integration import (
            adapter_transpiled_scripts,
            classify_scripts,
        )

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid = "abc123"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        node = _make_real_node("500", "DoorTrigger", guid)
        parsed_scene = _StubParsedScene(all_nodes={node.file_id: node})

        classification = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
        )

        assert cs_path.resolve() in classification.skip_paths
        match = classification.matches[cs_path.resolve()]
        assert match.class_name == "Door"
        assert len(match.bindings) == 1
        assert match.bindings[0].node_name == "DoorTrigger"

        scripts, gameplay_matches = adapter_transpiled_scripts(
            classification=classification,
            transpiled_script_cls=TranspiledScript,
        )
        assert len(scripts) == 1
        assert scripts[0].strategy == "gameplay_adapter"
        assert scripts[0].confidence == 1.0
        assert "Gameplay.run(" in scripts[0].luau_source
        assert scripts[0].output_filename == "Door.luau"

        assert len(gameplay_matches) == 1
        gm = gameplay_matches[0]
        assert gm.detector_name == "door"
        assert gm.node_name == "DoorTrigger"
        assert gm.node_file_id == "500"
        assert gm.component_file_id == "501"
        assert gm.script_path == str(cs_path)

    def test_no_scene_returns_empty_classification(self) -> None:
        from converter.gameplay.integration import classify_scripts

        result = classify_scripts(
            parsed_scene=None,
            guid_index=None,
            script_infos=[],
        )
        assert result.matches == {}
        assert result.skip_paths == set()

    def test_deny_list_suppresses_classification(self, tmp_path: Path) -> None:
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid = "abc123"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        node = _make_real_node("500", "DoorTrigger", guid)
        parsed_scene = _StubParsedScene(all_nodes={node.file_id: node})

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            deny_list=frozenset({"500"}),
        )
        assert result.matches == {}

    def test_multi_instance_equivalence_collects_bindings(
        self, tmp_path: Path,
    ) -> None:
        """Two scene nodes carrying the same Door class produce
        equivalent Behaviors, so they coalesce into one ClassMatch
        with two bindings — the operator can see both in the report.
        """
        from converter.gameplay.integration import classify_scripts

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid = "abc123"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        node_a = _make_real_node("500", "Door_A", guid, "5001")
        node_b = _make_real_node("501", "Door_B", guid, "5011")
        parsed_scene = _StubParsedScene(
            all_nodes={"500": node_a, "501": node_b},
        )

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
        )

        assert len(result.matches) == 1
        match = next(iter(result.matches.values()))
        assert len(match.bindings) == 2
        names = sorted(b.node_name for b in match.bindings)
        assert names == ["Door_A", "Door_B"]

    def test_divergent_per_node_behaviors_drop_only_that_class(
        self, tmp_path: Path,
    ) -> None:
        """Two classes match. One has divergent per-node shapes, the
        other is consistent. The divergent class falls into
        ``classification.divergent`` and the consistent class still
        emits — codex PR #73a-round-2 pinned that a divergent class
        no longer zeros the whole pass.
        """
        from converter.gameplay.capabilities import (
            ContainerResolver,
            LifetimePersistent,
            MovementAttributeDrivenTween,
            TriggerOnBoolAttribute,
        )
        from converter.gameplay.integration import classify_scripts

        class VariableOffsetDetector:
            name = "variable_door"

            def primary(self, node, component) -> bool:  # type: ignore[no-untyped-def]
                return _component_class(component) == "Door"

            def confirm(self, node, component, source) -> bool:  # type: ignore[no-untyped-def]
                return True

            def behavior(self, node, component, source):  # type: ignore[no-untyped-def]
                # Vary offset based on node file_id so the two nodes
                # produce non-equivalent Behaviors.
                offset = 4.0 if node.file_id == "500" else 8.0
                return Behavior(
                    unity_file_id=node.file_id,
                    diagnostic_name="Door",
                    capabilities=(
                        TriggerOnBoolAttribute(name="open"),
                        MovementAttributeDrivenTween(
                            target_offset_unity=(0.0, offset, 0.0),
                            open_duration=1.0,
                            close_duration=1.0,
                        ),
                        LifetimePersistent(),
                    ),
                    container_resolver=ContainerResolver(
                        kind="ascend_then_child", child_name="door",
                    ),
                )

        def _component_class(comp) -> str:  # type: ignore[no-untyped-def]
            if comp.component_type != "MonoBehaviour":
                return ""
            return str(comp.properties.get("_script_class_name", ""))

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid = "abc123"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        node_a = _make_real_node("500", "Door_A", guid, "5001")
        node_b = _make_real_node("501", "Door_B", guid, "5011")
        parsed_scene = _StubParsedScene(
            all_nodes={"500": node_a, "501": node_b},
        )

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            detectors=(VariableOffsetDetector(),),  # type: ignore[arg-type]
        )
        # Divergent class falls out of matches and into divergent[].
        assert result.matches == {}
        assert len(result.divergent) == 1
        assert result.divergent[0].class_name == "Door"
        assert "Door" in str(result.divergent[0].error)

    def test_divergent_class_does_not_kill_other_class(
        self, tmp_path: Path,
    ) -> None:
        """Two classes match. ClassX is consistent (one binding), ClassY
        diverges (two bindings with different shapes). ClassX still
        emits; ClassY drops into divergent[]. Pins round-2 P1 #2.
        """
        from converter.gameplay.capabilities import (
            ContainerResolver,
            LifetimePersistent,
            MovementAttributeDrivenTween,
            TriggerOnBoolAttribute,
        )
        from converter.gameplay.integration import classify_scripts

        def _make_behavior(node, offset: float):
            return Behavior(
                unity_file_id=node.file_id,
                diagnostic_name="DoorLike",
                capabilities=(
                    TriggerOnBoolAttribute(name="open"),
                    MovementAttributeDrivenTween(
                        target_offset_unity=(0.0, offset, 0.0),
                        open_duration=1.0,
                        close_duration=1.0,
                    ),
                    LifetimePersistent(),
                ),
                container_resolver=ContainerResolver(
                    kind="ascend_then_child", child_name="door",
                ),
            )

        class _ClassXDetector:
            name = "x"
            def primary(self, node, component) -> bool:  # type: ignore[no-untyped-def]
                return str(component.properties.get("_script_class_name", "")) == "ClassX"
            def confirm(self, node, component, source) -> bool:  # type: ignore[no-untyped-def]
                return True
            def behavior(self, node, component, source):  # type: ignore[no-untyped-def]
                return _make_behavior(node, 4.0)

        class _ClassYDetector:
            name = "y"
            def primary(self, node, component) -> bool:  # type: ignore[no-untyped-def]
                return str(component.properties.get("_script_class_name", "")) == "ClassY"
            def confirm(self, node, component, source) -> bool:  # type: ignore[no-untyped-def]
                return True
            def behavior(self, node, component, source):  # type: ignore[no-untyped-def]
                # ClassY diverges by node.
                offset = 4.0 if node.file_id == "600" else 8.0
                return _make_behavior(node, offset)

        cs_x = tmp_path / "ClassX.cs"
        cs_y = tmp_path / "ClassY.cs"
        cs_x.write_text(_DOOR_CSHARP, encoding="utf-8")
        cs_y.write_text(_DOOR_CSHARP, encoding="utf-8")
        info_x = _StubScriptInfo(path=cs_x, class_name="ClassX")
        info_y = _StubScriptInfo(path=cs_y, class_name="ClassY")
        guid_x, guid_y = "guid-x", "guid-y"
        guid_index = _StubGuidIndex(
            by_guid={guid_x: cs_x, guid_y: cs_y},
            by_path={cs_x.resolve(): guid_x, cs_y.resolve(): guid_y},
        )
        node_x = _make_real_node("500", "X_only", guid_x, "5001")
        node_y_a = _make_real_node("600", "Y_a", guid_y, "6001")
        node_y_b = _make_real_node("601", "Y_b", guid_y, "6011")
        parsed_scene = _StubParsedScene(
            all_nodes={"500": node_x, "600": node_y_a, "601": node_y_b},
        )

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info_x, info_y],  # type: ignore[list-item]
            detectors=(_ClassXDetector(), _ClassYDetector()),  # type: ignore[arg-type]
        )

        # ClassX still emits.
        assert cs_x.resolve() in result.matches
        # ClassY drops into divergent, NOT into matches.
        assert cs_y.resolve() not in result.matches
        assert len(result.divergent) == 1
        assert result.divergent[0].class_name == "ClassY"


# ---------------------------------------------------------------------------
# Detector protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_door_detector_satisfies_protocol(self) -> None:
        det: Detector = DoorDetector()
        assert det.name == "door"

    def test_all_detectors_present(self) -> None:
        # Slice-boundary pin. PR #73a registered ``door``; PR #73b adds
        # ``turret_bullet`` + ``plane_bullet``. Updating this test is
        # the deliberate signal that a new detector is shipping —
        # forgetting to register here means the deny-list and dispatch
        # would diverge.
        names = {d.name for d in ALL_DETECTORS}
        assert "door" in names
        assert "turret_bullet" in names
        assert "plane_bullet" in names


# ---------------------------------------------------------------------------
# Round-3: rehydrate marker / counters / report serialization
# ---------------------------------------------------------------------------

class TestAdapterStubMarker:
    """The first line of every emitted stub carries
    ``ADAPTER_STUB_MARKER``. Rehydrate-path runtime injection keys
    off this; user-authored scripts must not collide.
    """

    def test_marker_in_emit(self) -> None:
        from converter.gameplay.composer import ADAPTER_STUB_MARKER
        out = emit_behavior_stub(_door_behavior())
        assert ADAPTER_STUB_MARKER in out
        # Marker on the FIRST line specifically — scans key off the
        # leading comment.
        first_line = out.splitlines()[0]
        assert ADAPTER_STUB_MARKER in first_line

    def test_marker_is_distinctive(self) -> None:
        from converter.gameplay.composer import ADAPTER_STUB_MARKER
        # The marker should be weird enough that no plausible user
        # comment or string literal collides.
        assert "@@" in ADAPTER_STUB_MARKER


class _PlaceStub:
    """Minimal stand-in for ``RbxPlace`` shaped enough for the
    adapter-marker scan. Only the three script-bearing surfaces the
    scan walks are populated.
    """

    def __init__(
        self,
        scripts: list | None = None,
        workspace_parts: list | None = None,
        replicated_templates: list | None = None,
    ) -> None:
        self.scripts = scripts or []
        self.workspace_parts = workspace_parts or []
        self.replicated_templates = replicated_templates or []


class _PartStub:
    def __init__(
        self,
        name: str = "x",
        scripts: list | None = None,
        children: list | None = None,
    ) -> None:
        self.name = name
        self.scripts = scripts or []
        self.children = children or []


class _ScriptStub:
    def __init__(self, name: str = "s", source: str = "") -> None:
        self.name = name
        self.source = source


class TestRehydrateRuntimeInjection:
    """Exercise the real ``_place_carries_adapter_marker`` helper —
    if a contributor extracts a new helper or adds a new surface
    (e.g. ServerScriptService trees), the helper test fails loudly
    instead of papering over coverage with an inlined fixture.
    Codex PR #73a-round-4 caught a templates blind spot the prior
    inlined test missed.
    """

    def _marker_stub(self) -> _ScriptStub:
        from converter.gameplay.composer import ADAPTER_STUB_MARKER
        return _ScriptStub(
            name="Door",
            source=f"-- {ADAPTER_STUB_MARKER} Door unity_file_id=42\n",
        )

    def test_marker_scan_finds_top_level_script(self) -> None:
        from converter.pipeline import _place_carries_adapter_marker
        place = _PlaceStub(scripts=[self._marker_stub()])
        assert _place_carries_adapter_marker(place) is True

    def test_marker_scan_finds_workspace_bound_stub(self) -> None:
        from converter.pipeline import _place_carries_adapter_marker
        part = _PartStub(name="DoorPart", scripts=[self._marker_stub()])
        place = _PlaceStub(workspace_parts=[part])
        assert _place_carries_adapter_marker(place) is True

    def test_marker_scan_finds_nested_workspace_stub(self) -> None:
        from converter.pipeline import _place_carries_adapter_marker
        deep = _PartStub(name="Deep", scripts=[self._marker_stub()])
        mid = _PartStub(name="Mid", children=[deep])
        top = _PartStub(name="Top", children=[mid])
        place = _PlaceStub(workspace_parts=[top])
        assert _place_carries_adapter_marker(place) is True

    def test_marker_scan_finds_template_attached_stub(self) -> None:
        """The codex round-4 P1: adapter Scripts also live under
        ``rbx_place.replicated_templates[*].scripts`` via
        ``_attach_monobehaviour_scripts_to_templates``. The scan must
        walk that surface or rehydrate-path runtime injection misses.
        """
        from converter.pipeline import _place_carries_adapter_marker
        template_root = _PartStub(
            name="DoorTemplate",
            scripts=[self._marker_stub()],
        )
        place = _PlaceStub(replicated_templates=[template_root])
        assert _place_carries_adapter_marker(place) is True

    def test_marker_scan_returns_false_without_marker(self) -> None:
        """User content that mentions AutoGen / WaitForChild in a
        comment must NOT false-positive.
        """
        from converter.pipeline import _place_carries_adapter_marker
        user = _ScriptStub(
            name="UserScript",
            source=(
                'WaitForChild("AutoGen"):WaitForChild("Gameplay")  '
                '-- this is a user comment\n'
            ),
        )
        place = _PlaceStub(
            scripts=[user],
            workspace_parts=[_PartStub(scripts=[user])],
            replicated_templates=[_PartStub(scripts=[user])],
        )
        assert _place_carries_adapter_marker(place) is False

    def test_marker_scan_returns_false_on_empty_place(self) -> None:
        from converter.pipeline import _place_carries_adapter_marker
        assert _place_carries_adapter_marker(_PlaceStub()) is False
        # None-safe.
        assert _place_carries_adapter_marker(None) is False  # type: ignore[arg-type]


class TestTotalGameplayAdapterCounter:
    def test_adapter_scripts_increment_their_own_counter(
        self, tmp_path: Path,
    ) -> None:
        from converter.code_transpiler import TranspilationResult, TranspiledScript
        from converter.gameplay.integration import (
            adapter_transpiled_scripts,
            classify_scripts,
        )

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid = "abc"
        guid_index = _StubGuidIndex(
            by_guid={guid: cs_path},
            by_path={cs_path.resolve(): guid},
        )
        node = _make_real_node("500", "Door", guid)
        parsed_scene = _StubParsedScene(all_nodes={"500": node})

        classification = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
        )
        adapter_scripts, _matches = adapter_transpiled_scripts(
            classification=classification,
            transpiled_script_cls=TranspiledScript,
        )

        result = TranspilationResult()
        result.scripts.extend(adapter_scripts)
        result.total_transpiled += len(adapter_scripts)
        result.total_gameplay_adapter += len(adapter_scripts)

        counts = result.counts
        assert counts["gameplay_adapter"] == 1
        assert counts["rule_based"] == 0
        assert counts["ai"] == 0
        assert counts["total"] == 1


class TestReportSerialization:
    def test_gameplay_summary_fields_serialize(self) -> None:
        """The ConversionReport gameplay section must carry the
        operator-friendly fields the doc promises (codex round-2 P2).
        """
        from dataclasses import asdict
        from converter.report_generator import (
            ConversionReport,
            GameplayAdapterBinding,
            GameplayAdapterDivergence,
            GameplayAdapterDivergentBinding,
            GameplayAdapterSummary,
        )

        summary = GameplayAdapterSummary(
            enabled=True,
            total_classes_emitted=1,
            total_classes_divergent=1,
            total_bindings=2,
            bindings=[
                GameplayAdapterBinding(
                    detector_name="door",
                    diagnostic_name="Door",
                    target_class_name="Door",
                    node_name="DoorA",
                    node_file_id="500",
                    component_file_id="501",
                    script_path="/abs/Door.cs",
                    capability_kinds=["trigger.on_bool_attribute"],
                ),
            ],
            divergent_classes=[
                GameplayAdapterDivergence(
                    class_name="Other",
                    script_path="/abs/Other.cs",
                    detail="diverged",
                    binding_a=GameplayAdapterDivergentBinding(
                        node_name="A", node_file_id="600", component_file_id="601",
                    ),
                    binding_b=GameplayAdapterDivergentBinding(
                        node_name="B", node_file_id="610", component_file_id="611",
                    ),
                ),
            ],
        )
        report = ConversionReport(gameplay_adapters=summary)
        out = asdict(report)
        ga = out["gameplay_adapters"]
        assert ga["enabled"] is True
        assert ga["total_classes_emitted"] == 1
        assert ga["total_classes_divergent"] == 1
        assert ga["total_bindings"] == 2
        b0 = ga["bindings"][0]
        for key in (
            "detector_name", "diagnostic_name", "target_class_name",
            "node_name", "node_file_id", "component_file_id",
            "script_path", "capability_kinds",
        ):
            assert key in b0
        d0 = ga["divergent_classes"][0]
        assert "binding_a" in d0
        assert d0["binding_a"]["component_file_id"] == "601"
        assert d0["binding_b"]["node_file_id"] == "610"


class TestDivergentBehaviorsErrorShape:
    def test_carries_structured_bindings(self, tmp_path: Path) -> None:
        """DivergentBehaviorsError must carry NodeBinding records so
        the report can render component IDs (codex round-3 P2 fix).
        """
        from converter.gameplay.integration import classify_scripts
        from converter.gameplay.capabilities import (
            ContainerResolver,
            LifetimePersistent,
            MovementAttributeDrivenTween,
            TriggerOnBoolAttribute,
        )

        class _Varying:
            name = "vary"

            def primary(self, node, component) -> bool:  # type: ignore[no-untyped-def]
                return str(component.properties.get("_script_class_name", "")) == "Door"

            def confirm(self, node, component, source) -> bool:  # type: ignore[no-untyped-def]
                return True

            def behavior(self, node, component, source):  # type: ignore[no-untyped-def]
                offset = 4.0 if node.file_id == "500" else 8.0
                return Behavior(
                    unity_file_id=node.file_id,
                    diagnostic_name="Door",
                    capabilities=(
                        TriggerOnBoolAttribute(name="open"),
                        MovementAttributeDrivenTween(
                            target_offset_unity=(0.0, offset, 0.0),
                            open_duration=1.0,
                            close_duration=1.0,
                        ),
                        LifetimePersistent(),
                    ),
                    container_resolver=ContainerResolver(
                        kind="ascend_then_child", child_name="door",
                    ),
                )

        cs_path = tmp_path / "Door.cs"
        cs_path.write_text(_DOOR_CSHARP, encoding="utf-8")
        info = _StubScriptInfo(path=cs_path, class_name="Door")
        guid_index = _StubGuidIndex(
            by_guid={"g": cs_path}, by_path={cs_path.resolve(): "g"},
        )
        node_a = _make_real_node("500", "Door_A", "g", "5001")
        node_b = _make_real_node("501", "Door_B", "g", "5011")
        parsed_scene = _StubParsedScene(
            all_nodes={"500": node_a, "501": node_b},
        )

        result = classify_scripts(
            parsed_scene=parsed_scene,  # type: ignore[arg-type]
            guid_index=guid_index,  # type: ignore[arg-type]
            script_infos=[info],  # type: ignore[list-item]
            detectors=(_Varying(),),  # type: ignore[arg-type]
        )
        assert len(result.divergent) == 1
        err = result.divergent[0].error
        # Both bindings carry NodeBinding shape, not plain strings.
        assert err.binding_a.node_name == "Door_A"
        assert err.binding_a.unity_file_id == "500"
        assert err.binding_a.component_file_id == "5001"
        assert err.binding_b.node_name == "Door_B"
        assert err.binding_b.unity_file_id == "501"
        assert err.binding_b.component_file_id == "5011"


class TestStaleStateReset:
    def test_pipeline_zeros_adapter_state_each_pass(self) -> None:
        """transpile_scripts must reset state.gameplay_matches and
        state.gameplay_divergent_classes at the top of each pass —
        otherwise a prior run's matches poison the rehydrate scan
        and the report (codex round-3 P1).
        """
        import inspect
        from converter.pipeline import Pipeline
        src = inspect.getsource(Pipeline.transpile_scripts)
        # The reset must happen before any analyze/classify call.
        reset_idx = src.find("self.state.gameplay_matches = []")
        analyze_idx = src.find("analyze_all_scripts(")
        assert reset_idx > 0
        assert "self.state.gameplay_divergent_classes = []" in src
        # Both lines exist before the AI transpile call.
        ai_idx = src.find("transpile_scripts(")
        # transpile_scripts function name appears in the docstring + call;
        # use the inner call's signature ``unity_project_path=`` as the
        # anchor.
        ai_call = src.find("unity_project_path=self.unity_project_path")
        assert reset_idx < ai_call
