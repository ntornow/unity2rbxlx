"""Phase 4.9 — extract_serialized_field_refs + serialize_for_context."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.serialized_field_extractor import (
    _is_object_ref,
    _process_mono_properties,
    extract_serialized_field_refs,
    serialize_for_context,
)


class _StubGuidIndex:
    """Returns a caller-configured path for each GUID."""

    def __init__(self, mapping: dict[str, Path]):
        self._mapping = mapping

    def resolve(self, guid: str) -> Path | None:
        return self._mapping.get(guid)


def _component(component_type: str, properties: dict) -> SimpleNamespace:
    return SimpleNamespace(component_type=component_type, properties=properties)


class TestIsObjectRef:
    def test_valid_ref(self):
        assert _is_object_ref({"guid": "a" * 32})

    def test_zero_guid_rejected(self):
        assert not _is_object_ref({"guid": "0" * 32})

    def test_non_dict(self):
        assert not _is_object_ref("not a dict")
        assert not _is_object_ref(None)

    def test_missing_guid(self):
        assert not _is_object_ref({})


class TestProcessMonoProperties:
    def test_prefab_ref_recorded(self, tmp_path):
        script_path = tmp_path / "Player.cs"
        prefab_path = tmp_path / "Rifle.prefab"
        script_path.write_text("// stub")
        prefab_path.write_text("// stub")
        guid_index = _StubGuidIndex({
            "player_guid": script_path,
            "rifle_guid": prefab_path,
        })
        props = {
            "m_Script": {"guid": "player_guid"},
            "riflePrefab": {"guid": "rifle_guid"},
        }
        result: dict = {}
        _process_mono_properties(props, guid_index, result)
        assert result == {script_path: {"riflePrefab": "Rifle"}}

    def test_audio_ref_prefixed(self, tmp_path):
        script_path = tmp_path / "Player.cs"
        audio_path = tmp_path / "shot.ogg"
        script_path.write_text("")
        audio_path.write_text("")
        guid_index = _StubGuidIndex({
            "player_guid": script_path,
            "audio_guid": audio_path,
        })
        props = {
            "m_Script": {"guid": "player_guid"},
            "shootSound": {"guid": "audio_guid"},
        }
        result: dict = {}
        _process_mono_properties(props, guid_index, result)
        assert result[script_path]["shootSound"] == f"audio:{audio_path}"

    def test_internal_props_skipped(self, tmp_path):
        """m_-prefixed / engine-internal fields must not appear as refs."""
        script_path = tmp_path / "P.cs"
        script_path.write_text("")
        guid_index = _StubGuidIndex({"gs": script_path})
        props = {
            "m_Script": {"guid": "gs"},
            "m_GameObject": {"guid": "some_random"},
            "m_Enabled": 1,
        }
        result: dict = {}
        _process_mono_properties(props, guid_index, result)
        # No non-internal field written → no entry for this script.
        assert result == {}

    def test_first_binding_wins(self, tmp_path):
        """Duplicate field names (shouldn't happen, but be safe) keep first."""
        script_path = tmp_path / "P.cs"
        prefab_a = tmp_path / "A.prefab"
        prefab_b = tmp_path / "B.prefab"
        for p in (script_path, prefab_a, prefab_b):
            p.write_text("")
        guid_index = _StubGuidIndex({
            "script": script_path, "a": prefab_a, "b": prefab_b,
        })
        result: dict = {}
        _process_mono_properties(
            {"m_Script": {"guid": "script"}, "target": {"guid": "a"}},
            guid_index, result,
        )
        _process_mono_properties(
            {"m_Script": {"guid": "script"}, "target": {"guid": "b"}},
            guid_index, result,
        )
        assert result[script_path]["target"] == "A"

    def test_non_cs_script_skipped(self, tmp_path):
        """m_Script resolves to non-.cs → skip entirely."""
        dll = tmp_path / "Plugin.dll"
        dll.write_text("")
        guid_index = _StubGuidIndex({"plugin": dll})
        result: dict = {}
        _process_mono_properties(
            {"m_Script": {"guid": "plugin"}, "field": {"guid": "anything"}},
            guid_index, result,
        )
        assert result == {}


class TestExtractSerializedFieldRefs:
    def test_scene_and_prefab_both_walked(self, tmp_path):
        script_path = tmp_path / "Foo.cs"
        scene_prefab = tmp_path / "FromScene.prefab"
        prefab_prefab = tmp_path / "FromPrefab.prefab"
        for p in (script_path, scene_prefab, prefab_prefab):
            p.write_text("")
        guid_index = _StubGuidIndex({
            "foo_guid": script_path,
            "scene_ref": scene_prefab,
            "prefab_ref": prefab_prefab,
        })

        scene_node = SimpleNamespace(components=[
            _component("MonoBehaviour", {
                "m_Script": {"guid": "foo_guid"},
                "sceneField": {"guid": "scene_ref"},
            }),
        ])
        scene = SimpleNamespace(all_nodes={"1": scene_node})

        prefab_node = SimpleNamespace(
            components=[_component("MonoBehaviour", {
                "m_Script": {"guid": "foo_guid"},
                "prefabField": {"guid": "prefab_ref"},
            })],
            children=[],
        )
        template = SimpleNamespace(root=prefab_node)
        prefab_library = SimpleNamespace(prefabs=[template])

        result = extract_serialized_field_refs(
            [scene], prefab_library, guid_index,
        )
        assert result[script_path] == {
            "sceneField": "FromScene",
            "prefabField": "FromPrefab",
        }

    def test_handles_none_prefab_library(self, tmp_path):
        guid_index = _StubGuidIndex({})
        result = extract_serialized_field_refs([], None, guid_index)
        assert result == {}

    def test_handles_none_guid_index(self):
        result = extract_serialized_field_refs([], None, None)
        assert result == {}


class TestSerializeForContext:
    def test_paths_relative_to_project_root(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        script_path = project_root / "Assets" / "Scripts" / "Foo.cs"
        script_path.parent.mkdir(parents=True)
        script_path.write_text("")
        refs = {script_path: {"field": "Prefab"}}
        out = serialize_for_context(refs, project_root=project_root)
        assert "Assets/Scripts/Foo.cs" in list(out.keys())[0].replace("\\", "/")
        assert out[list(out)[0]] == {"field": "Prefab"}

    def test_falls_back_to_absolute_when_not_under_root(self, tmp_path):
        outside = tmp_path / "outside.cs"
        outside.write_text("")
        other_root = tmp_path / "elsewhere"
        other_root.mkdir()
        refs = {outside: {"field": "X"}}
        out = serialize_for_context(refs, project_root=other_root)
        # Not under other_root → absolute path.
        key = list(out)[0]
        assert Path(key).is_absolute() or outside.name in key

    def test_no_project_root(self):
        refs = {Path("/tmp/abs/path.cs"): {"field": "X"}}
        out = serialize_for_context(refs)
        assert out == {"/tmp/abs/path.cs": {"field": "X"}}


class TestCodexFix4MPrefixFields:
    """Codex P1 #4: don't drop [SerializeField] private m_foo fields."""

    def test_m_prefixed_user_field_captured(self, tmp_path):
        """Private serialized fields like ``m_prefab`` / ``m_shootSound``
        are legitimate inspector-assigned refs. They must land in the
        output, not be silently filtered as engine-internal.
        """
        script_path = tmp_path / "Weapon.cs"
        prefab_path = tmp_path / "Bullet.prefab"
        script_path.write_text("")
        prefab_path.write_text("")
        guid_index = _StubGuidIndex({
            "wep_guid": script_path,
            "pfb_guid": prefab_path,
        })
        props = {
            "m_Script": {"guid": "wep_guid"},
            "m_bulletPrefab": {"guid": "pfb_guid"},  # user field
            "m_GameObject": {"guid": "something"},    # engine-internal, must skip
        }
        result: dict = {}
        _process_mono_properties(props, guid_index, result)
        assert result == {script_path: {"m_bulletPrefab": "Bullet"}}
