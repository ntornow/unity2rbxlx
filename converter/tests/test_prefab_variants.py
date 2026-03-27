"""
test_prefab_variants.py -- Tests for prefab variant chain resolution.
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path
from typing import Any

import pytest

from core.unity_types import (
    PrefabComponent,
    PrefabNode,
    PrefabTemplate,
    PrefabLibrary,
)
from unity.prefab_parser import (
    _parse_single_prefab,
    _apply_variant_modifications,
    _resolve_variant_chain,
    _set_nested_property,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base_template() -> PrefabTemplate:
    """Create a simple base prefab template for testing."""
    root = PrefabNode(
        name="BasePrefab",
        file_id="100",
        active=True,
        position=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
        components=[
            PrefabComponent(
                component_type="Transform",
                file_id="101",
                properties={
                    "m_LocalPosition": {"x": 0, "y": 0, "z": 0},
                    "m_LocalRotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                    "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                },
            ),
        ],
    )
    child = PrefabNode(
        name="SubPart",
        file_id="200",
        active=True,
        position=(0.0, 1.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(0.5, 0.5, 0.5),
        components=[
            PrefabComponent(
                component_type="Transform",
                file_id="201",
                properties={
                    "m_LocalPosition": {"x": 0, "y": 1, "z": 0},
                    "m_LocalRotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                    "m_LocalScale": {"x": 0.5, "y": 0.5, "z": 0.5},
                },
            ),
        ],
    )
    child.parent_file_id = "100"
    root.children.append(child)

    template = PrefabTemplate(
        prefab_path=Path("/fake/Base.prefab"),
        name="BasePrefab",
        root=root,
        all_nodes={"100": root, "200": child},
        variant_resolved=True,
    )
    return template


# ---------------------------------------------------------------------------
# Tests: _parse_single_prefab detects variants
# ---------------------------------------------------------------------------

class TestParseVariantDetection:
    def test_base_prefab_not_variant(self):
        """A normal prefab file should not be detected as a variant."""
        template = _parse_single_prefab(FIXTURES_DIR / "simple_prefab.yaml")
        assert not template.is_variant
        assert template.source_prefab_guid is None
        assert template.root is not None
        assert template.root.name == "TestPrefab"

    def test_variant_prefab_detected(self):
        """A prefab with a PrefabInstance document should be detected as a variant."""
        template = _parse_single_prefab(FIXTURES_DIR / "variant_prefab.yaml")
        assert template.is_variant
        assert template.source_prefab_guid == "aabbccdd11223344aabbccdd11223344"
        assert len(template.variant_modifications) == 6
        # Variant file has no GameObjects of its own -- just the PrefabInstance
        assert template.root is None


# ---------------------------------------------------------------------------
# Tests: _apply_variant_modifications
# ---------------------------------------------------------------------------

class TestApplyVariantModifications:
    def test_name_override(self):
        """m_Name modification should rename the target node."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "100"},
                "propertyPath": "m_Name",
                "value": "RenamedPrefab",
                "objectReference": {"fileID": 0},
            },
        ]
        _apply_variant_modifications(template, modifications)
        assert template.root.name == "RenamedPrefab"

    def test_position_override(self):
        """m_LocalPosition modifications should update the node's position."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "101"},  # Transform component
                "propertyPath": "m_LocalPosition.x",
                "value": "5.0",
                "objectReference": {"fileID": 0},
            },
            {
                "target": {"fileID": "101"},
                "propertyPath": "m_LocalPosition.y",
                "value": "10.0",
                "objectReference": {"fileID": 0},
            },
        ]
        _apply_variant_modifications(template, modifications)
        assert template.root.position == (5.0, 10.0, 0.0)

    def test_scale_override(self):
        """m_LocalScale modifications should update the node's scale."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "101"},
                "propertyPath": "m_LocalScale.x",
                "value": "2.0",
            },
            {
                "target": {"fileID": "101"},
                "propertyPath": "m_LocalScale.y",
                "value": "2.0",
            },
            {
                "target": {"fileID": "101"},
                "propertyPath": "m_LocalScale.z",
                "value": "2.0",
            },
        ]
        _apply_variant_modifications(template, modifications)
        assert template.root.scale == (2.0, 2.0, 2.0)

    def test_child_node_modification(self):
        """Modifications targeting a child's transform should update the child."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "201"},  # Child transform
                "propertyPath": "m_LocalPosition.y",
                "value": "3.0",
            },
        ]
        _apply_variant_modifications(template, modifications)
        child = template.all_nodes["200"]
        assert child.position == (0.0, 3.0, 0.0)

    def test_is_active_override(self):
        """m_IsActive modification should update the node's active flag."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "200"},
                "propertyPath": "m_IsActive",
                "value": "0",
            },
        ]
        _apply_variant_modifications(template, modifications)
        assert template.all_nodes["200"].active is False

    def test_component_property_override(self):
        """Arbitrary component property changes should update the properties dict."""
        template = _make_base_template()
        # Add a MonoBehaviour component with a custom property
        mono = PrefabComponent(
            component_type="MonoBehaviour",
            file_id="300",
            properties={"speed": 5.0, "damage": 10},
        )
        template.root.components.append(mono)

        modifications = [
            {
                "target": {"fileID": "300"},
                "propertyPath": "speed",
                "value": "15.0",
            },
        ]
        _apply_variant_modifications(template, modifications)
        assert template.root.components[-1].properties["speed"] == "15.0"

    def test_empty_modifications(self):
        """Empty modifications should not change anything."""
        template = _make_base_template()
        original_name = template.root.name
        _apply_variant_modifications(template, [])
        assert template.root.name == original_name

    def test_unknown_target_does_not_crash(self):
        """Modifications targeting unknown fileIDs should be silently skipped."""
        template = _make_base_template()
        modifications = [
            {
                "target": {"fileID": "999999"},
                "propertyPath": "m_Name",
                "value": "Ghost",
            },
        ]
        # Should not raise
        _apply_variant_modifications(template, modifications)
        assert template.root.name == "BasePrefab"


# ---------------------------------------------------------------------------
# Tests: _set_nested_property
# ---------------------------------------------------------------------------

class TestSetNestedProperty:
    def test_simple_key(self):
        props = {"speed": 5}
        _set_nested_property(props, "speed", "10")
        assert props["speed"] == "10"

    def test_dotted_path(self):
        props = {"m_LocalPosition": {"x": 0, "y": 0, "z": 0}}
        _set_nested_property(props, "m_LocalPosition.x", "3.5")
        assert props["m_LocalPosition"]["x"] == "3.5"

    def test_array_data_path(self):
        props = {"m_Materials": [{"guid": "aaa"}, {"guid": "bbb"}]}
        _set_nested_property(props, "m_Materials.Array.data[1]", None, {"guid": "ccc"})
        assert props["m_Materials"][1] == {"guid": "ccc"}

    def test_creates_missing_intermediate(self):
        props = {}
        _set_nested_property(props, "m_Foo.bar", "42")
        assert props["m_Foo"]["bar"] == "42"

    def test_object_reference_override(self):
        props = {"m_Mesh": {"fileID": 0, "guid": "old"}}
        _set_nested_property(props, "m_Mesh", None, {"fileID": 123, "guid": "new"})
        assert props["m_Mesh"]["guid"] == "new"


# ---------------------------------------------------------------------------
# Tests: _resolve_variant_chain
# ---------------------------------------------------------------------------

class TestResolveVariantChain:
    def test_simple_variant(self):
        """A single variant should inherit the source's node tree with modifications."""
        base = _make_base_template()
        base_guid = "aabbccdd"

        variant = PrefabTemplate(
            prefab_path=Path("/fake/Variant.prefab"),
            name="VariantPrefab",
            is_variant=True,
            source_prefab_guid=base_guid,
            variant_modifications=[
                {
                    "target": {"fileID": "100"},
                    "propertyPath": "m_Name",
                    "value": "OverriddenName",
                },
                {
                    "target": {"fileID": "101"},
                    "propertyPath": "m_LocalPosition.x",
                    "value": "5.0",
                },
            ],
        )

        by_guid = {base_guid: base}
        _resolve_variant_chain(variant, by_guid)

        assert variant.variant_resolved
        assert variant.root is not None
        assert variant.root.name == "OverriddenName"
        assert variant.root.position == (5.0, 0.0, 0.0)
        # Children should be inherited
        assert len(variant.root.children) == 1
        assert variant.root.children[0].name == "SubPart"

    def test_chained_variants(self):
        """Variant3 -> Variant2 -> Base should resolve the full chain."""
        base = _make_base_template()
        base_guid = "base1111"

        variant2 = PrefabTemplate(
            prefab_path=Path("/fake/Variant2.prefab"),
            name="Variant2",
            is_variant=True,
            source_prefab_guid=base_guid,
            variant_modifications=[
                {
                    "target": {"fileID": "100"},
                    "propertyPath": "m_Name",
                    "value": "Stage2",
                },
            ],
        )
        variant2_guid = "var22222"

        variant3 = PrefabTemplate(
            prefab_path=Path("/fake/Variant3.prefab"),
            name="Variant3",
            is_variant=True,
            source_prefab_guid=variant2_guid,
            variant_modifications=[
                {
                    "target": {"fileID": "101"},
                    "propertyPath": "m_LocalScale.x",
                    "value": "3.0",
                },
            ],
        )

        by_guid = {base_guid: base, variant2_guid: variant2}
        _resolve_variant_chain(variant3, by_guid)

        assert variant3.variant_resolved
        assert variant3.root is not None
        # Name came from variant2's modification
        assert variant3.root.name == "Stage2"
        # Scale came from variant3's modification
        assert variant3.root.scale[0] == 3.0
        # Original base scale for y/z
        assert variant3.root.scale[1] == 1.0

    def test_cycle_detection(self):
        """Circular variant references should not infinite-loop."""
        a = PrefabTemplate(
            prefab_path=Path("/fake/A.prefab"),
            name="A",
            is_variant=True,
            source_prefab_guid="guid_b",
        )
        b = PrefabTemplate(
            prefab_path=Path("/fake/B.prefab"),
            name="B",
            is_variant=True,
            source_prefab_guid="guid_a",
        )
        by_guid = {"guid_a": a, "guid_b": b}
        # Should not raise or infinite-loop
        _resolve_variant_chain(a, by_guid)
        assert a.variant_resolved

    def test_missing_source(self):
        """A variant whose source GUID is not found should resolve gracefully."""
        variant = PrefabTemplate(
            prefab_path=Path("/fake/Orphan.prefab"),
            name="Orphan",
            is_variant=True,
            source_prefab_guid="nonexistent",
        )
        _resolve_variant_chain(variant, {})
        assert variant.variant_resolved
        assert variant.root is None  # No source to merge from

    def test_source_modifications_dont_affect_base(self):
        """Modifications to a variant should not mutate the base template."""
        base = _make_base_template()
        original_name = base.root.name
        base_guid = "baseguid"

        variant = PrefabTemplate(
            prefab_path=Path("/fake/V.prefab"),
            name="V",
            is_variant=True,
            source_prefab_guid=base_guid,
            variant_modifications=[
                {
                    "target": {"fileID": "100"},
                    "propertyPath": "m_Name",
                    "value": "Changed",
                },
            ],
        )
        by_guid = {base_guid: base}
        _resolve_variant_chain(variant, by_guid)

        # Variant should have the new name
        assert variant.root.name == "Changed"
        # Base should be untouched
        assert base.root.name == original_name

    def test_already_resolved_skipped(self):
        """A template that's already resolved should not be re-processed."""
        template = _make_base_template()
        template.is_variant = True
        template.variant_resolved = True
        template.source_prefab_guid = "something"

        # Should be a no-op
        _resolve_variant_chain(template, {})
        assert template.root.name == "BasePrefab"
