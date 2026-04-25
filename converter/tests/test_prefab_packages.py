"""Phase 4.10 — generate_prefab_packages."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.prefab_packages import (
    PrefabPackagesResult,
    _collect_referenced_prefab_names,
    _SPAWNER_LUAU,
    generate_prefab_packages,
    write_packages_manifest,
)
from core.roblox_types import RbxPart


def _prefab_template(name: str, has_root: bool = True):
    """Build a minimal PrefabTemplate-like object for generator tests."""
    root = SimpleNamespace(
        name=name,
        position=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
        mesh_guid=None,
        mesh_file_id=None,
        components=[],
        children=[],
        active=True,
        file_id="1",
        from_prefab_instance=False,
        source_prefab_name=None,
    )
    return SimpleNamespace(
        name=name,
        root=(root if has_root else None),
    )


def _library(*prefabs):
    return SimpleNamespace(prefabs=list(prefabs))


class TestCollectReferencedNames:
    def test_gathers_prefab_refs_from_serialized_map(self):
        refs = {
            "Assets/Player.cs": {"riflePrefab": "Rifle", "feedbackPrefab": "Flare"},
            "Assets/Enemy.cs":  {"spawnTarget": "Enemy"},
        }
        names = _collect_referenced_prefab_names(refs)
        assert names == {"Rifle", "Flare", "Enemy"}

    def test_audio_refs_filtered_out(self):
        refs = {
            "Assets/X.cs": {
                "prefab": "Item",
                "sound": "audio:/abs/path/shot.ogg",
            },
        }
        assert _collect_referenced_prefab_names(refs) == {"Item"}

    def test_none_returns_empty(self):
        assert _collect_referenced_prefab_names(None) == set()

    def test_empty_returns_empty(self):
        assert _collect_referenced_prefab_names({}) == set()


class TestGenerateBasic:
    def test_no_library_no_output(self):
        result = generate_prefab_packages(None, None, None)
        assert result.templates == []
        assert result.spawner_script is None

    def test_empty_library_no_output(self):
        result = generate_prefab_packages(_library(), None, None)
        assert result.templates == []

    def test_unreferenced_prefab_not_emitted(self, monkeypatch):
        """Default: only prefabs named in serialized_field_refs emit."""
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"), _prefab_template("Unused"))
        refs = {"Assets/P.cs": {"riflePrefab": "Rifle"}}
        result = generate_prefab_packages(lib, refs, guid_index=None)
        emitted = {t.name for t in result.templates}
        assert emitted == {"Rifle"}
        assert result.manifest["emitted_names"] == ["Rifle"]

    def test_include_all_bypasses_filter(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("A"), _prefab_template("B"))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert {t.name for t in result.templates} == {"A", "B"}


class TestUnconvertedEntries:
    def test_null_root_records_entry(self):
        lib = _library(_prefab_template("Broken", has_root=False))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert result.templates == []
        assert len(result.unconverted) == 1
        assert result.unconverted[0]["category"] == "prefab_package"
        assert result.unconverted[0]["item"] == "Broken"

    def test_converter_returning_none_records_entry(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: None,
        )
        lib = _library(_prefab_template("Empty"))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert result.templates == []
        assert len(result.unconverted) == 1
        assert "returned None" in result.unconverted[0]["reason"]

    def test_converter_raising_recorded_not_fatal(self, monkeypatch):
        def _raise(node, **_):
            raise RuntimeError("boom")
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node", _raise,
        )
        lib = _library(_prefab_template("Exploding"), _prefab_template("Fine"))
        # Second prefab uses the raising stub too since we monkeypatched
        # the whole symbol — but the important assertion is that one
        # failing prefab doesn't stop the loop.
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert len(result.unconverted) == 2


class TestSpawnerScript:
    def test_emitted_when_templates_present(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"riflePrefab": "Rifle"}},
            guid_index=None,
        )
        assert result.spawner_script is not None
        assert result.spawner_script.name == "PrefabSpawner"
        assert result.spawner_script.script_type == "ModuleScript"
        assert result.spawner_script.source_path == "packages/PrefabSpawner.luau"
        # Source must contain the WaitForChild pattern scripts rely on.
        assert "Templates:WaitForChild" in _SPAWNER_LUAU or "WaitForChild" in _SPAWNER_LUAU
        assert "ReplicatedStorage" in result.spawner_script.source
        assert "inline-over-runtime-wrappers.md" in result.spawner_script.source

    def test_absent_when_no_templates(self):
        """No prefabs → no spawner."""
        lib = _library()
        result = generate_prefab_packages(lib, None, guid_index=None)
        assert result.spawner_script is None


class TestManifestPersistence:
    def test_write_creates_packages_dir(self, tmp_path):
        manifest = {"total_templates": 2, "emitted_names": ["A", "B"]}
        path = write_packages_manifest(tmp_path, manifest)
        assert path.exists()
        assert (tmp_path / "packages").is_dir()
        data = json.loads(path.read_text())
        assert data == manifest

    def test_missing_referenced_reported(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"))
        # Script references Rifle AND Grenade — only Rifle is in the
        # library. Manifest must surface Grenade as missing.
        refs = {"Assets/P.cs": {"a": "Rifle", "b": "Grenade"}}
        result = generate_prefab_packages(lib, refs, guid_index=None)
        assert result.manifest["emitted_names"] == ["Rifle"]
        assert result.manifest["referenced_but_missing"] == ["Grenade"]


class TestReplicatedTemplatesField:
    def test_rbxplace_has_field(self):
        from core.roblox_types import RbxPlace
        p = RbxPlace()
        assert p.replicated_templates == []
        assert isinstance(p.replicated_templates, list)
