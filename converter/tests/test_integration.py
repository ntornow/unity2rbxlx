"""Integration tests for the full conversion pipeline."""

import json
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SIMPLEFPS_DIR = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"
BOATATTACK_DIR = Path(__file__).parent.parent.parent / "test_projects" / "BoatAttack"


def _has_project(path: Path) -> bool:
    return path.exists() and (path / "Assets").exists()


@pytest.mark.skipif(not _has_project(SIMPLEFPS_DIR), reason="SimpleFPS project not found")
class TestSimpleFPSConversion:
    """Integration tests using the SimpleFPS test project."""

    def test_scene_parsing(self):
        from unity.scene_parser import parse_scene
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        assert len(scene.roots) > 0
        assert len(scene.all_nodes) > 0
        assert len(scene.prefab_instances) > 0

    def test_prefab_parsing(self):
        from unity.prefab_parser import parse_prefabs
        prefabs = parse_prefabs(SIMPLEFPS_DIR)
        assert len(prefabs.by_name) > 0
        assert "Turret" in prefabs.by_name
        assert "crate01" in prefabs.by_name

    def test_guid_resolution(self):
        from unity.guid_resolver import build_guid_index
        idx = build_guid_index(SIMPLEFPS_DIR)
        # Should have entries for FBX, materials, textures
        assert idx.total_resolved > 100

    def test_material_mapping(self):
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.material_mapper import map_materials

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        mat_mappings = map_materials(
            SIMPLEFPS_DIR, idx,
            scene.referenced_material_guids,
            Path(tempfile.mkdtemp()),
        )
        assert len(mat_mappings) > 0

    def test_material_parser_old_format(self):
        """Test that old-format Unity materials parse correctly."""
        from converter.material_mapper import _parse_mat_yaml, _normalize_tex_envs

        # WispySkyboxMat2 uses old format with repeated data: keys
        mat_path = SIMPLEFPS_DIR / "Assets/AssetPack/Wispy Sky/Materials/WispySkyboxMat2.mat"
        if not mat_path.exists():
            pytest.skip("Material file not found")

        raw = mat_path.read_text(errors="replace")
        mat_data = _parse_mat_yaml(raw)
        assert mat_data is not None

        saved = mat_data.get("m_SavedProperties", {})
        tex_envs = _normalize_tex_envs(saved.get("m_TexEnvs", []))
        # Should have skybox face textures
        assert "_FrontTex" in tex_envs or "_MainTex" in tex_envs

    def test_scene_conversion(self):
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        place = convert_scene(scene, idx)

        assert len(place.workspace_parts) > 0
        assert place.lighting is not None

    def test_fbx_as_prefab_converted(self):
        """FBX files used as prefabs should produce MeshParts."""
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        place = convert_scene(scene, idx)

        # Find parts with FBX-prefab names
        def find_parts(parts, name_fragment):
            count = 0
            for p in parts:
                if name_fragment in p.name:
                    count += 1
                count += find_parts(p.children, name_fragment)
            return count

        # barbedWireRoll has 13 instances, should be present
        barbed = find_parts(place.workspace_parts, "barbedWireRoll")
        assert barbed >= 10, f"Expected >=10 barbedWireRoll parts, got {barbed}"

    def test_terrain_detected(self):
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        place = convert_scene(scene, idx)

        assert len(place.terrains) == 1
        assert place.terrains[0].size == (1000.0, 600.0, 1000.0)

    def test_directional_light_clock_time(self):
        """Directional light rotation should set ClockTime."""
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        place = convert_scene(scene, idx)

        # SimpleFPS has a directional light at ~30° pitch -> ClockTime ~14
        assert 12.0 <= place.lighting.clock_time <= 16.0, \
            f"ClockTime {place.lighting.clock_time} should be afternoon (12-16)"

    def test_skybox_extraction(self):
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        assert scene.skybox_material_guid is not None

    def test_animation_conversion(self):
        from unity.guid_resolver import build_guid_index
        from converter.animation_converter import convert_animations

        idx = build_guid_index(SIMPLEFPS_DIR)
        result = convert_animations(SIMPLEFPS_DIR, idx)
        assert result.total_clips > 0
        assert len(result.generated_scripts) > 0

    def test_script_transpilation(self):
        from unity.script_analyzer import analyze_all_scripts
        from converter.code_transpiler import transpile_scripts

        scripts = analyze_all_scripts(SIMPLEFPS_DIR)
        assert len(scripts) > 0

        # Use cached results (no AI needed for test)
        tr = transpile_scripts(SIMPLEFPS_DIR, scripts, use_ai=True, api_key="")
        assert tr.total_transpiled > 0

    def test_gameplay_pattern_fixes(self):
        from converter.luau_validator import fix_gameplay_patterns

        # Test pickup detection fix
        source = 'if obj.Name == "Pickup" and obj:IsA("BasePart") then'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "GetAttribute" in fixed or "string.find" in fixed
        assert len(fixes) > 0

        # Test turret name matching fix
        source2 = 'if obj.Name == "Turret" and obj:IsA("Model") then'
        fixed2, fixes2 = fix_gameplay_patterns("test.luau", source2)
        assert "string.find" in fixed2

    def test_fps_client_generator(self):
        from converter.fps_client_generator import generate_hud_client_script

        script = generate_hud_client_script()
        assert "HUD" in script.source
        assert "FindFirstChild" in script.source
        # Should not reference "Pause" via WaitForChild (Canvas-converted HUD may not have it)
        assert "WaitForChild(\"Pause\")" not in script.source


@pytest.mark.skipif(not _has_project(BOATATTACK_DIR), reason="BoatAttack project not found")
class TestBoatAttackConversion:
    """Integration tests using the BoatAttack test project."""

    def test_scene_parsing(self):
        from unity.scene_parser import parse_scene
        scene_path = BOATATTACK_DIR / "Assets/scenes/static_Island.unity"
        if not scene_path.exists():
            pytest.skip("Scene file not found")
        scene = parse_scene(scene_path)
        assert len(scene.roots) > 0

    def test_full_conversion(self):
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        scene_path = BOATATTACK_DIR / "Assets/scenes/static_Island.unity"
        if not scene_path.exists():
            pytest.skip("Scene file not found")
        idx = build_guid_index(BOATATTACK_DIR)
        scene = parse_scene(scene_path)
        place = convert_scene(scene, idx)
        assert len(place.workspace_parts) > 0


class TestRbxlxOutputQuality:
    """Tests that verify the quality of the generated rbxlx output."""

    @pytest.fixture
    def simplefps_rbxlx(self, tmp_path):
        """Generate a SimpleFPS rbxlx for testing."""
        if not _has_project(SIMPLEFPS_DIR):
            pytest.skip("SimpleFPS project not found")

        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene
        from roblox.rbxlx_writer import write_rbxlx

        idx = build_guid_index(SIMPLEFPS_DIR)
        scene = parse_scene(SIMPLEFPS_DIR / "Assets/Scenes/main.unity")
        place = convert_scene(scene, idx)

        rbxlx_path = tmp_path / "test.rbxlx"
        write_rbxlx(place, rbxlx_path)
        return rbxlx_path

    def test_valid_xml(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        assert tree.getroot().tag == "roblox"

    def test_no_invalid_classes(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        valid = {
            "Workspace", "Terrain", "Part", "MeshPart", "Model", "SpawnLocation",
            "PointLight", "SpotLight", "SurfaceLight", "Sound",
            "Script", "LocalScript", "ModuleScript",
            "ScreenGui", "Frame", "TextLabel", "TextButton", "ImageLabel",
            "SurfaceAppearance", "Sky", "Camera",
            "Lighting", "ServerScriptService", "ReplicatedStorage", "ServerStorage",
            "StarterGui", "StarterPlayer", "StarterPlayerScripts", "StarterCharacterScripts",
            "RemoteEvent", "ParticleEmitter",
            "Trail", "Beam", "Attachment",
            "UIListLayout", "UIGridLayout",
            "WeldConstraint", "HingeConstraint", "SpringConstraint", "BallSocketConstraint",
            "BloomEffect", "ColorCorrectionEffect", "DepthOfFieldEffect",
            "SunRaysEffect", "Atmosphere",
        }
        for item in tree.iter("Item"):
            cls = item.get("class", "")
            assert cls in valid, f"Invalid class: {cls}"

    def test_no_local_file_paths(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        for item in tree.iter("Content"):
            url = item.find("url")
            if url is not None and url.text:
                assert "/" not in url.text or "rbxassetid" in url.text, \
                    f"Local path found: {url.text[:60]}"

    def test_materials_are_tokens(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        string_mats = sum(1 for e in tree.iter("string") if e.get("name") == "Material")
        assert string_mats == 0, f"Found {string_mats} string Material properties (should be tokens)"

    def test_has_smooth_surfaces(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        top = sum(1 for e in tree.iter("token") if e.get("name") == "TopSurface")
        assert top > 0, "No TopSurface tokens found"

    def test_streaming_disabled(self, simplefps_rbxlx):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        for item in tree.iter("Item"):
            if item.get("class") == "Workspace":
                props = item.find("Properties")
                stream = props.find('.//bool[@name="StreamingEnabled"]') if props is not None else None
                assert stream is not None and stream.text == "false", "StreamingEnabled should be false"
                break

    def test_no_collider_compounding(self, simplefps_rbxlx):
        """Collider sizes should not compound exponentially."""
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        # Check that no Part named "Collider" exceeds reasonable bounds
        for item in tree.iter("Item"):
            if item.get("class") == "Part":
                props = item.find("Properties")
                if props is None:
                    continue
                name = ""
                for s in props.iter("string"):
                    if s.get("name") == "Name":
                        name = s.text or ""
                        break
                if name == "Collider":
                    for vec in props.iter("Vector3"):
                        if vec.get("name") == "size":
                            x = float(vec.find("X").text)
                            y = float(vec.find("Y").text)
                            z = float(vec.find("Z").text)
                            biggest = max(x, y, z)
                            assert biggest < 500, f"Collider too large: ({x}, {y}, {z})"
                            break


    def test_material_variety(self, simplefps_rbxlx):
        """Scene should have multiple material types, not all Plastic."""
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(simplefps_rbxlx))
        materials = set()
        for item in tree.iter("Item"):
            if item.get("class") in ("Part", "MeshPart"):
                props = item.find("Properties")
                if props is None:
                    continue
                for tok in props.iter("token"):
                    if tok.get("name") == "Material":
                        materials.add(tok.text)
                        break
        # Should have at least 3 different materials (Plastic, Wood, Metal/Concrete)
        assert len(materials) >= 3, f"Only {len(materials)} material types: {materials}"


GAMEKIT3D_DIR = Path(__file__).parent.parent.parent / "test_projects" / "Gamekit3D"


@pytest.mark.skipif(not _has_project(GAMEKIT3D_DIR), reason="Gamekit3D project not found")
class TestGamekit3DConversion:
    """Integration tests using the Gamekit3D test project."""

    def test_scene_parsing(self):
        from unity.scene_parser import parse_scene
        scene_path = GAMEKIT3D_DIR / "Assets" / "3DGamekit" / "Scenes" / "Level1.unity"
        if not scene_path.exists():
            # Try finding any .unity scene
            scenes = list(GAMEKIT3D_DIR.rglob("*.unity"))
            if not scenes:
                pytest.skip("No scene files found")
            scene_path = scenes[0]
        scene = parse_scene(scene_path)
        assert len(scene.roots) > 0

    def test_no_conversion_crash(self):
        """Verify the full conversion pipeline completes without errors."""
        from unity.scene_parser import parse_scene
        from unity.guid_resolver import build_guid_index
        from converter.scene_converter import convert_scene

        scenes = list(GAMEKIT3D_DIR.rglob("*.unity"))
        text_scenes = []
        for s in scenes:
            try:
                header = s.read_bytes()[:10]
                if b"%YAML" in header:
                    text_scenes.append(s)
            except OSError:
                pass

        if not text_scenes:
            pytest.skip("No text YAML scenes found")

        idx = build_guid_index(GAMEKIT3D_DIR)
        scene = parse_scene(text_scenes[0])
        place = convert_scene(scene, idx)
        assert len(place.workspace_parts) > 0


class TestGameplayPatternFixes:
    """Tests for the fix_gameplay_patterns post-transpilation fixer."""

    def test_non_gameplay_not_modified(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = "local x = 5\nprint(x)"
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert fixed == source
        assert len(fixes) == 0

    def test_pickup_detection_fix(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'if obj.Name == "Pickup" and obj:IsA("BasePart") then'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "GetAttribute" in fixed or "string.find" in fixed

    def test_turret_name_pattern(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'if obj.Name == "Turret" and obj:IsA("Model") then'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "string.find" in fixed

    def test_mine_name_pattern(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'if obj.Name == "Mine" and obj:IsA("BasePart") then'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "string.find" in fixed

    def test_placeholder_sound_removal(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'sound.SoundId = "rbxassetid://1905339338"'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "1905339338" not in fixed

    def test_pickup_destruction_model(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'local function setupPickup(part)\n\tpart:Destroy()\nend'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "Model" in fixed

    def test_item_name_from_attribute(self):
        from converter.luau_validator import fix_gameplay_patterns
        source = 'local function setupPickup(part)\n\tlocal itemName = part.Name\nend'
        fixed, fixes = fix_gameplay_patterns("test.luau", source)
        assert "ItemType" in fixed or "GetAttribute" in fixed


class TestLuauValidator:
    """Tests for Luau script validation and fixup."""

    def test_startup_delay_for_getdescendants(self):
        from converter.luau_validator import validate_and_fix
        source = 'local RunService = game:GetService("RunService")\n\nfor _, obj in workspace:GetDescendants() do\n\tprint(obj.Name)\nend'
        fixed, fixes = validate_and_fix("test", source)
        assert "task.wait" in fixed
        assert fixed.index("task.wait") < fixed.index("GetDescendants")

    def test_no_delay_when_already_waited(self):
        from converter.luau_validator import validate_and_fix
        source = 'task.wait(2)\nfor _, obj in workspace:GetDescendants() do\n\tprint(obj)\nend'
        fixed, fixes = validate_and_fix("test", source)
        # Should not add a second task.wait
        assert fixed.count("task.wait") == 1

    def test_strip_leading_prose(self):
        from converter.luau_validator import validate_and_fix
        source = "Here is the converted script:\nlocal x = 1"
        fixed, fixes = validate_and_fix("test", source)
        assert fixed.startswith("local x = 1")
        assert len(fixes) > 0

    def test_fix_null_keyword(self):
        from converter.luau_validator import validate_and_fix
        source = "local x = null\nif x == null then end"
        fixed, fixes = validate_and_fix("test", source)
        assert "nil" in fixed
        assert "null" not in fixed

    def test_fix_this_keyword(self):
        from converter.luau_validator import validate_and_fix
        source = "local name = this.Name\nthis.Position = Vector3.new(0,0,0)"
        fixed, fixes = validate_and_fix("test", source)
        assert "this." not in fixed
        assert "script.Parent." in fixed

    def test_fix_bool_case(self):
        from converter.luau_validator import validate_and_fix
        source = "local x = True\nlocal y = False"
        fixed, fixes = validate_and_fix("test", source)
        assert "True" not in fixed
        assert "False" not in fixed
        assert "true" in fixed
        assert "false" in fixed

    def test_plugin_only_properties(self):
        from converter.luau_validator import validate_and_fix
        source = "workspace.StreamingEnabled = true"
        fixed, fixes = validate_and_fix("test", source)
        assert "DISABLED" in fixed

    def test_runtime_modulescript_creation_disabled(self):
        """Instance.new('ModuleScript') with .Source should be fully commented out."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local m = Instance.new("ModuleScript")\n'
            'm.Name = "Config"\n'
            'm.Source = [[\n'
            'local Config = {}\n'
            'return Config\n'
            ']]\n'
            'm.Parent = game.ReplicatedStorage\n'
            'print("done")'
        )
        fixed, fixes = validate_and_fix("test", source)
        assert "cannot create scripts at runtime" in fixed
        # The closing ]] should be commented out, not left dangling
        for line in fixed.split("\n"):
            stripped = line.strip()
            if stripped == "]]":
                assert False, "Dangling ]] found — should be commented out"
        # The print after the block should remain
        assert 'print("done")' in fixed

    def test_runtime_script_source_variable_commented(self):
        """When .Source = varName, the variable's multiline string def should also be commented."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local shakeSource = [[\n'
            'local camera = workspace.CurrentCamera\n'
            'print("shaking")\n'
            ']]\n'
            '\n'
            'local function setup()\n'
            '  local s = Instance.new("LocalScript")\n'
            '  s.Name = "Shaker"\n'
            '  s.Source = shakeSource\n'
            '  s.Parent = game.StarterPlayer\n'
            'end\n'
            'setup()'
        )
        fixed, fixes = validate_and_fix("test", source)
        # The multiline string variable should be commented out
        for line in fixed.split("\n"):
            stripped = line.strip()
            if stripped == "]]":
                assert False, "Dangling ]] found — source variable should be commented out"
        # The setup() call after the block should remain
        assert 'setup()' in fixed
        assert "DISABLED" in fixed

    def test_valid_code_unchanged(self):
        from converter.luau_validator import validate_and_fix
        source = "local x = 1\nprint(x)"
        fixed, fixes = validate_and_fix("test", source)
        assert fixed == source
        assert len(fixes) == 0

    def test_fix_string_to_hash(self):
        """Animator.StringToHash('Name') should be replaced with just 'Name'."""
        from converter.luau_validator import validate_and_fix
        source = 'local hash = Animator.StringToHash("Running")\nmodel:SetAttribute(hash, true)'
        fixed, _ = validate_and_fix("test", source)
        assert 'local hash = "Running"' in fixed
        assert "StringToHash" not in fixed

    def test_fix_deprecated_body_movers(self):
        """BodyVelocity/BodyGyro should be replaced with modern equivalents."""
        from converter.luau_validator import validate_and_fix
        source = 'local bv = Instance.new("BodyVelocity")\nlocal bg = Instance.new("BodyGyro")'
        fixed, fixes = validate_and_fix("test", source)
        assert "LinearVelocity" in fixed
        assert "AlignOrientation" in fixed
        assert "BodyVelocity" not in fixed.replace("-- was BodyVelocity", "")
        assert "BodyGyro" not in fixed.replace("-- was BodyGyro", "")

    def test_fix_not_equality_precedence(self):
        """'not x == y' should become 'x ~= y' (Luau precedence bug)."""
        from converter.luau_validator import validate_and_fix
        source = 'if not message:sub(1, 1) == "/" then return end'
        fixed, _ = validate_and_fix("test", source)
        assert 'message:sub(1, 1) ~= "/"' in fixed

    def test_fix_not_inequality_precedence(self):
        """'not x ~= y' should become 'x == y'."""
        from converter.luau_validator import validate_and_fix
        source = "if not value ~= nil then return end"
        fixed, _ = validate_and_fix("test", source)
        assert "value == nil" in fixed

    def test_fix_semicolons(self):
        from converter.luau_validator import validate_and_fix
        source = "local x = 1;\nprint(x);"
        fixed, _ = validate_and_fix("test", source)
        assert ";" not in fixed

    def test_fix_compound_assignment(self):
        from converter.luau_validator import validate_and_fix
        source = "health += 10"
        fixed, _ = validate_and_fix("test", source)
        assert "+=" not in fixed
        assert "health = health + 10" in fixed

    def test_fix_new_vector3(self):
        from converter.luau_validator import validate_and_fix
        source = "local v = new Vector3(1, 2, 3)"
        fixed, _ = validate_and_fix("test", source)
        assert "Vector3.new(1, 2, 3)" in fixed

    def test_fix_destroy_dot_syntax(self):
        from converter.luau_validator import validate_and_fix
        source = "part.Destroy()"
        fixed, _ = validate_and_fix("test", source)
        assert "part:Destroy()" in fixed

    def test_fix_gameobject_reference(self):
        from converter.luau_validator import validate_and_fix
        source = "local obj = part.gameObject"
        fixed, _ = validate_and_fix("test", source)
        assert "gameObject" not in fixed

    def test_fix_intensity_to_brightness(self):
        from converter.luau_validator import validate_and_fix
        source = "m_Light.intensity = 2 * math.noise(x)"
        fixed, fixes = validate_and_fix("test", source)
        assert ".Brightness" in fixed
        assert ".intensity" not in fixed

    def test_fix_input_getaxis_mouse(self):
        from converter.luau_validator import validate_and_fix
        source = 'local x = UserInputService:GetGamepadState(Enum.UserInputType.Gamepad1)("MouseX")'
        fixed, fixes = validate_and_fix("test", source)
        assert "GetMouseDelta().X" in fixed
        assert "GetGamepadState" not in fixed

    def test_fix_input_getaxis_horizontal(self):
        from converter.luau_validator import validate_and_fix
        source = 'local h = UserInputService:GetGamepadState(Enum.UserInputType.Gamepad1)("Horizontal")'
        fixed, fixes = validate_and_fix("test", source)
        assert "IsKeyDown" in fixed
        assert "GetGamepadState" not in fixed

    def test_event_subscription_numeric_not_converted(self):
        """curHealth = curHealth + healAmount should NOT become curHealth:Connect(healAmount)."""
        from converter.luau_validator import validate_and_fix
        source = "curHealth = curHealth + healAmount"
        fixed, _ = validate_and_fix("test", source)
        assert ":Connect(" not in fixed
        assert "curHealth = curHealth + healAmount" in fixed

    def test_event_subscription_actual_event(self):
        """HealthUpdate = HealthUpdate + handler SHOULD become HealthUpdate:Connect(handler)."""
        from converter.luau_validator import validate_and_fix
        source = "HealthUpdate = HealthUpdate + UpdateHealth"
        fixed, _ = validate_and_fix("test", source)
        assert ":Connect(UpdateHealth)" in fixed

    def test_fix_cframe_position_assignment(self):
        from converter.luau_validator import validate_and_fix
        source = "script.Parent.CFrame.Position = Vector3.new(0, 0, 0)"
        fixed, fixes = validate_and_fix("test", source)
        assert ".CFrame.Position =" not in fixed
        assert "script.Parent.Position =" in fixed

    def test_fix_audio_volume(self):
        from converter.luau_validator import validate_and_fix
        source = "audio.volume = 0.5"
        fixed, _ = validate_and_fix("test", source)
        assert ".Volume" in fixed
        assert ".volume" not in fixed

    def test_fix_audio_loop(self):
        from converter.luau_validator import validate_and_fix
        source = "audio.loop = true"
        fixed, _ = validate_and_fix("test", source)
        assert ".Looped" in fixed
        assert ".loop " not in fixed

    def test_fix_audio_clip_length(self):
        from converter.luau_validator import validate_and_fix
        source = "local dur = audio.clip.length"
        fixed, _ = validate_and_fix("test", source)
        assert "audio.TimeLength" in fixed

    def test_fix_audio_is_playing(self):
        from converter.luau_validator import validate_and_fix
        source = "if audio.isPlaying then"
        fixed, _ = validate_and_fix("test", source)
        assert ".IsPlaying" in fixed

    def test_fix_runservice_stepped(self):
        from converter.luau_validator import validate_and_fix
        source = 'RunService.Stepped:Connect(function(dt)\n\tprint(dt)\nend)'
        fixed, _ = validate_and_fix("test", source)
        assert "RunService.Heartbeat" in fixed
        assert "RunService.Stepped" not in fixed

    def test_fix_math_clamp01(self):
        from converter.luau_validator import validate_and_fix
        source = "local x = math.clamp01(value)"
        fixed, _ = validate_and_fix("test", source)
        assert "math.clamp(value, 0, 1)" in fixed
        assert "clamp01" not in fixed

    def test_fix_gameobject_destroy_with_delay(self):
        from converter.luau_validator import validate_and_fix
        source = "GameObject:Destroy(obj, 2.0)"
        fixed, _ = validate_and_fix("test", source)
        assert "Debris" in fixed
        assert "AddItem" in fixed

    def test_fix_gameobject_destroy_no_delay(self):
        from converter.luau_validator import validate_and_fix
        source = "GameObject:Destroy(obj)"
        fixed, _ = validate_and_fix("test", source)
        assert "obj:Destroy()" in fixed

    def test_fix_play_delayed(self):
        from converter.luau_validator import validate_and_fix
        source = "audio.PlayDelayed(0.5)"
        fixed, _ = validate_and_fix("test", source)
        assert "task.delay" in fixed
        assert ":Play()" in fixed

    def test_fix_color_lowercase(self):
        from converter.luau_validator import validate_and_fix
        source = "light.color = Color.new(1, 0, 0)"
        fixed, _ = validate_and_fix("test", source)
        assert ".Color" in fixed
        assert ".color" not in fixed

    def test_fix_find_first_child_object_of_type(self):
        from converter.luau_validator import validate_and_fix
        source = 'workspace:FindFirstChildObjectOfType()'
        fixed, _ = validate_and_fix("test", source)
        assert "FindFirstChildWhichIsA" in fixed

    def test_fix_float_isnan(self):
        from converter.luau_validator import validate_and_fix
        source = "local smp = (if float.IsNaN(x1) then 0 else x1)"
        fixed, _ = validate_and_fix("test", source)
        assert "(x1 ~= x1)" in fixed
        assert "float.IsNaN" not in fixed

    def test_fix_color_lerp(self):
        from converter.luau_validator import validate_and_fix
        source = "light.Color = Color.Lerp(colorA, colorB, t)"
        fixed, _ = validate_and_fix("test", source)
        assert "colorA:Lerp(colorB, t)" in fixed

    def test_fix_nonalloc_methods(self):
        from converter.luau_validator import validate_and_fix
        source = "workspace:GetPartBoundsInRadiusNonAlloc(pos, radius, results)"
        fixed, _ = validate_and_fix("test", source)
        assert "GetPartBoundsInRadius(" in fixed
        assert "NonAlloc" not in fixed

    def test_fix_max_distance(self):
        from converter.luau_validator import validate_and_fix
        source = "audio.maxDistance = 50"
        fixed, _ = validate_and_fix("test", source)
        assert ".RollOffMaxDistance" in fixed

    def test_fix_waitforseconds_type_decl(self):
        from converter.luau_validator import validate_and_fix
        source = "        WaitForSeconds delay"
        fixed, _ = validate_and_fix("test", source)
        assert "local delay" in fixed
        assert "WaitForSeconds" not in fixed

    def test_fix_string_empty(self):
        from converter.luau_validator import validate_and_fix
        source = 'local text = string.Empty'
        fixed, _ = validate_and_fix("test", source)
        assert '""' in fixed
        assert "string.Empty" not in fixed

    def test_fix_tostring_method(self):
        from converter.luau_validator import validate_and_fix
        source = 'local s = obj.Name.ToString()'
        fixed, _ = validate_and_fix("test", source)
        assert "tostring(obj.Name)" in fixed
        assert ".ToString()" not in fixed

    def test_fix_continue_keyword(self):
        from converter.luau_validator import validate_and_fix
        source = 'for _, item in items do\n    if not item then\n        continue\n    end\n    process(item)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert "continue" not in fixed or "-- continue" in fixed

    def test_fix_starts_with(self):
        from converter.luau_validator import validate_and_fix
        source = 'if line.StartsWith("#") then'
        fixed, _ = validate_and_fix("test", source)
        assert "string.sub" in fixed
        assert ".StartsWith" not in fixed

    def test_fix_substring(self):
        from converter.luau_validator import validate_and_fix
        source = 'local cmd = msg.Substring(1, length)'
        fixed, _ = validate_and_fix("test", source)
        assert "string.sub" in fixed
        assert ".Substring" not in fixed

    def test_fix_trim(self):
        from converter.luau_validator import validate_and_fix
        source = 'local cleaned = text.Trim()'
        fixed, _ = validate_and_fix("test", source)
        assert "string.match" in fixed
        assert ".Trim()" not in fixed

    def test_fix_int_parse(self):
        from converter.luau_validator import validate_and_fix
        source = 'local n = int.Parse(input)'
        fixed, _ = validate_and_fix("test", source)
        assert "tonumber(input)" in fixed
        assert "int.Parse" not in fixed

    def test_fix_int_tryparse(self):
        from converter.luau_validator import validate_and_fix
        source = 'if int.TryParse(str, out result) then'
        fixed, _ = validate_and_fix("test", source)
        assert "tonumber" in fixed
        assert "TryParse" not in fixed

    def test_fix_dt_in_task_wait_loop(self):
        from converter.luau_validator import validate_and_fix
        source = 'while running do\n    pos = pos + vel * dt\n    task.wait()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert "dt = task.wait()" in fixed

    def test_fix_property_getter_inline(self):
        from converter.luau_validator import validate_and_fix
        source = '    local audio = nil -- AudioSource\n    local function get_audio() return script.Parent:FindFirstChildWhichIsA("Sound") end'
        fixed, fixes = validate_and_fix("test", source)
        assert 'local audio = script.Parent:FindFirstChildWhichIsA("Sound")' in fixed
        assert 'get_audio' not in fixed
        assert any("Inlined" in f for f in fixes)

    def test_fix_unity_class_names(self):
        from converter.luau_validator import validate_and_fix
        source = 'local audio = script.Parent:FindFirstChildWhichIsA("AudioSource")'
        fixed, _ = validate_and_fix("test", source)
        assert '"Sound"' in fixed
        assert '"AudioSource"' not in fixed


class TestMeshSizing:
    """Tests for mesh size computation."""

    def test_returns_none_without_data(self):
        from converter.scene_converter import _compute_mesh_size
        from unittest.mock import MagicMock
        idx = MagicMock()
        idx.resolve.return_value = None
        result = _compute_mesh_size((1, 1, 1), "guid", idx, {})
        assert result is None

    def test_computes_size_with_native_data(self):
        from converter.scene_converter import _compute_mesh_size
        from unittest.mock import MagicMock
        from pathlib import Path
        idx = MagicMock()
        idx.resolve.return_value = Path("test.fbx")
        idx.resolve_relative.return_value = Path("test.fbx")
        mns = {"test.fbx": (10.0, 20.0, 10.0)}
        result = _compute_mesh_size((1, 1, 1), "guid", idx, mns)
        assert result is not None
        size, init = result
        assert init == (10.0, 20.0, 10.0)
        assert all(s > 0 for s in size)

    def test_fbx_import_scale_turret(self):
        """Verify turret_01.fbx returns correct import scale (USF=100 → 1.0)."""
        from converter.scene_converter import _get_fbx_import_scale
        from unity.guid_resolver import build_guid_index
        from pathlib import Path

        proj = Path("../test_projects/SimpleFPS")
        if not proj.exists():
            pytest.skip("SimpleFPS not found")

        idx = build_guid_index(proj)
        # Find turret mesh GUID
        for guid, entry in idx.guid_to_entry.items():
            if "turret_01.fbx" in str(entry.relative_path):
                scale = _get_fbx_import_scale(guid, idx)
                assert abs(scale - 1.0) < 0.01, f"Turret import scale should be 1.0, got {scale}"
                return
        pytest.skip("turret_01.fbx GUID not found")

    def test_fbx_import_scale_totem(self):
        """Verify totem_01.fbx returns correct import scale (globalScale=100)."""
        from converter.scene_converter import _get_fbx_import_scale
        from unity.guid_resolver import build_guid_index
        from pathlib import Path

        proj = Path("../test_projects/SimpleFPS")
        if not proj.exists():
            pytest.skip("SimpleFPS not found")

        idx = build_guid_index(proj)
        for guid, entry in idx.guid_to_entry.items():
            if "totem_01.fbx" in str(entry.relative_path):
                scale = _get_fbx_import_scale(guid, idx)
                assert abs(scale - 100.0) < 0.1, f"Totem import scale should be 100.0, got {scale}"
                return
        pytest.skip("totem_01.fbx GUID not found")


class TestCLIPipeline:
    """Tests for the CLI pipeline end-to-end."""

    @pytest.mark.skipif(not _has_project(SIMPLEFPS_DIR), reason="SimpleFPS not found")
    def test_convert_command(self, tmp_path):
        """Test the full convert command."""
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "u2r.py", "convert", str(SIMPLEFPS_DIR),
             "-o", str(tmp_path / "output"), "--no-upload"],
            capture_output=True, text=True,
            timeout=120,
        )
        assert result.returncode == 0
        assert "Conversion complete" in result.stdout
        assert (tmp_path / "output" / "converted_place.rbxlx").exists()

    @pytest.mark.skipif(not _has_project(SIMPLEFPS_DIR), reason="SimpleFPS not found")
    def test_validate_command(self, tmp_path):
        """Test the validate command on a fresh conversion."""
        import subprocess, sys
        # Convert first
        subprocess.run(
            [sys.executable, "u2r.py", "convert", str(SIMPLEFPS_DIR),
             "-o", str(tmp_path / "output"), "--no-upload"],
            capture_output=True, text=True,
            timeout=120,
        )
        # Then validate
        rbxlx = tmp_path / "output" / "converted_place.rbxlx"
        result = subprocess.run(
            [sys.executable, "u2r.py", "validate", str(rbxlx)],
            capture_output=True, text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "No issues found" in result.stdout or "No errors found" in result.stdout


class TestEdgeCases:
    """Tests for edge case inputs and error handling."""

    def test_empty_scene_file(self, tmp_path):
        from unity.scene_parser import parse_scene
        empty = tmp_path / "empty.unity"
        empty.write_text("")
        scene = parse_scene(empty)
        assert len(scene.roots) == 0

    def test_missing_scene_file(self):
        from unity.scene_parser import parse_scene
        with pytest.raises(FileNotFoundError):
            parse_scene("/nonexistent/scene.unity")

    def test_special_characters_in_part_name(self, tmp_path):
        from core.roblox_types import RbxPart, RbxPlace
        from roblox.rbxlx_writer import write_rbxlx
        import xml.etree.ElementTree as ET

        place = RbxPlace(workspace_parts=[
            RbxPart(name='Test <Part> & "Quotes"', class_name="Part"),
        ])
        rbxlx = tmp_path / "test.rbxlx"
        write_rbxlx(place, rbxlx)
        tree = ET.parse(str(rbxlx))
        assert tree.getroot().tag == "roblox"

    def test_unicode_part_name(self, tmp_path):
        from core.roblox_types import RbxPart, RbxPlace
        from roblox.rbxlx_writer import write_rbxlx
        import xml.etree.ElementTree as ET

        place = RbxPlace(workspace_parts=[
            RbxPart(name="Ünïcödé Pärt", class_name="Part"),
        ])
        rbxlx = tmp_path / "test.rbxlx"
        write_rbxlx(place, rbxlx)
        tree = ET.parse(str(rbxlx))
        assert tree.getroot().tag == "roblox"

    def test_extreme_values(self, tmp_path):
        from core.roblox_types import RbxPart, RbxPlace, RbxCFrame
        from roblox.rbxlx_writer import write_rbxlx
        import xml.etree.ElementTree as ET

        place = RbxPlace(workspace_parts=[
            RbxPart(
                name="Extreme",
                class_name="Part",
                cframe=RbxCFrame(x=999999, y=-999999, z=0.00001),
                size=(0.001, 99999, 0.001),
            ),
        ])
        rbxlx = tmp_path / "test.rbxlx"
        write_rbxlx(place, rbxlx)
        tree = ET.parse(str(rbxlx))
        assert tree.getroot().tag == "roblox"
