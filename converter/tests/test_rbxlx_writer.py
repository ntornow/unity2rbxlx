"""
test_rbxlx_writer.py -- Tests for RBXLX file generation.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import (
    RbxPart, RbxCFrame, RbxScript, RbxSurfaceAppearance,
    RbxLight, RbxSound, RbxPlace, RbxLightingConfig,
    RbxScreenGui, RbxUIElement,
)


class TestRbxlxWriter:
    def test_write_empty_place(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = RbxPlace()
        output = tmp_path / "test.rbxlx"
        result = write_rbxlx(place, output)
        assert output.exists()
        assert result["parts_written"] == 1  # Default SpawnLocation auto-created
        # Verify valid XML
        tree = ET.parse(output)
        assert tree.getroot().tag == "roblox"

    def test_write_single_part(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        part = RbxPart(
            name="TestPart",
            cframe=RbxCFrame(x=1, y=2, z=3),
            size=(4, 1, 2),
            color=(1, 0, 0),
        )
        place = RbxPlace(workspace_parts=[part])
        output = tmp_path / "test.rbxlx"
        result = write_rbxlx(place, output)
        assert result["parts_written"] == 2  # TestPart + default SpawnLocation

        # Check XML structure
        tree = ET.parse(output)
        root = tree.getroot()
        # Find Workspace
        workspace = None
        for item in root.iter("Item"):
            if item.get("class") == "Workspace":
                workspace = item
                break
        assert workspace is not None

    def test_write_with_scripts(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        script = RbxScript(
            name="TestScript",
            source='print("Hello from Roblox!")',
            script_type="Script",
        )
        place = RbxPlace(scripts=[script])
        output = tmp_path / "test.rbxlx"
        result = write_rbxlx(place, output)
        assert result["scripts_written"] == 1

    def test_write_with_lighting(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        lighting = RbxLightingConfig(
            brightness=2.5,
            ambient=(0.3, 0.3, 0.3),
            clock_time=14.0,
        )
        place = RbxPlace(lighting=lighting)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)

        content = output.read_text()
        assert "Lighting" in content

    def test_write_with_ui(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        gui = RbxScreenGui(
            name="GameUI",
            elements=[
                RbxUIElement(
                    class_name="TextLabel",
                    name="ScoreLabel",
                    text="Score: 0",
                    size=(0, 200, 0, 50),
                ),
            ],
        )
        place = RbxPlace(screen_guis=[gui])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        content = output.read_text()
        assert "ScreenGui" in content

    def test_button_onclick_event_wiring(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        btn = RbxUIElement(
            class_name="TextButton",
            name="StartBtn",
            text="Start",
            size=(0, 200, 0, 50),
            on_click_handlers=[{"method": "StartGame", "target_file_id": "123"}],
            attributes={"_OnClick": "StartGame"},
        )
        gui = RbxScreenGui(
            name="MenuUI",
            elements=[btn],
        )
        place = RbxPlace(screen_guis=[gui])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        content = output.read_text()
        assert "UIEventWiring" in content
        assert "StartGame" in content
        assert "Activated:Connect" in content

    def test_cframe_serialization(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        part = RbxPart(
            name="RotatedPart",
            cframe=RbxCFrame(
                x=10, y=5, z=-3,
                r00=0, r01=0, r02=1,
                r10=0, r11=1, r12=0,
                r20=-1, r21=0, r22=0,
            ),
        )
        place = RbxPlace(workspace_parts=[part])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        content = output.read_text()
        assert "10" in content  # X position
        assert "CoordinateFrame" in content or "CFrame" in content

    def test_nested_parts(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        child = RbxPart(name="Child", size=(1, 1, 1))
        parent = RbxPart(
            name="Parent",
            class_name="Model",
            children=[child],
        )
        place = RbxPlace(workspace_parts=[parent])
        output = tmp_path / "test.rbxlx"
        result = write_rbxlx(place, output)
        assert result["parts_written"] == 3  # parent + child + default SpawnLocation

    def test_cdata_wrapping_all_scripts(self, tmp_path):
        """All ProtectedString elements must have CDATA wrapping for valid XML."""
        from roblox.rbxlx_writer import write_rbxlx
        scripts = []
        for i in range(5):
            scripts.append(RbxScript(
                name=f"Script{i}",
                source=f'local x = {i}\nprint("hello <world>")\n-- comment',
                script_type="Script",
            ))
        place = RbxPlace(scripts=scripts)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        content = output.read_text()
        # All ProtectedString elements should have CDATA
        import re
        opens = re.findall(r'<ProtectedString[^/][^>]*>', content)
        cdatas = re.findall(r'<!\[CDATA\[', content)
        assert len(opens) == len(cdatas), f"ProtectedString count ({len(opens)}) != CDATA count ({len(cdatas)})"
        # Verify valid XML
        tree = ET.parse(output)
        assert tree.getroot().tag == "roblox"

    def test_cdata_wrapping_with_special_chars(self, tmp_path):
        """Scripts with XML-special characters must be properly CDATA-wrapped."""
        from roblox.rbxlx_writer import write_rbxlx
        scripts = [RbxScript(
            name="SpecialChars",
            source='local s = "<color=blue>test</color>"\nlocal t = "a > b & c < d"',
            script_type="Script",
        )]
        place = RbxPlace(scripts=scripts)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        # Verify valid XML
        tree = ET.parse(output)
        assert tree.getroot().tag == "roblox"


class TestSpriteAtlasRendering:
    """Test sprite atlas rect → SurfaceGui > ImageLabel rendering."""

    def test_sprite_with_rect_uses_surface_gui(self, tmp_path):
        """When sprite has rect attributes, should use SurfaceGui+ImageLabel, not Decal."""
        from roblox.rbxlx_writer import write_rbxlx
        part = RbxPart(
            name="AtlasSprite",
            size=(2.0, 0.1, 2.0),
        )
        part.attributes["_SpriteTextureId"] = "rbxassetid://12345"
        part.attributes["_SpriteRectX"] = 10.0
        part.attributes["_SpriteRectY"] = 20.0
        part.attributes["_SpriteRectW"] = 64.0
        part.attributes["_SpriteRectH"] = 32.0

        place = RbxPlace(workspace_parts=[part])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)

        tree = ET.parse(output)
        root = tree.getroot()

        # Find SurfaceGuis with SpriteSurfaceGui in name
        surface_guis = []
        for item in root.iter("Item"):
            if item.get("class") == "SurfaceGui":
                name_el = item.find("Properties/string[@name='Name']")
                if name_el is not None and "SpriteSurfaceGui" in (name_el.text or ""):
                    surface_guis.append(item)

        assert len(surface_guis) == 2, f"Expected 2 SpriteSurfaceGuis (Front+Back), got {len(surface_guis)}"

        # Check that ImageLabel children exist with correct properties
        for sg in surface_guis:
            image_labels = [i for i in sg.iter("Item") if i.get("class") == "ImageLabel"]
            assert len(image_labels) == 1
            il = image_labels[0]
            il_props = il.find("Properties")

            # Check ImageRectOffset
            offset = il_props.find("Vector2[@name='ImageRectOffset']")
            assert offset is not None
            assert offset.find("X").text == "10.0"
            assert offset.find("Y").text == "20.0"

            # Check ImageRectSize
            rect_size = il_props.find("Vector2[@name='ImageRectSize']")
            assert rect_size is not None
            assert rect_size.find("X").text == "64.0"
            assert rect_size.find("Y").text == "32.0"

        # Should NOT have any sprite Decals
        sprite_decals = []
        for item in root.iter("Item"):
            if item.get("class") == "Decal":
                name_el = item.find("Properties/string[@name='Name']")
                if name_el is not None and "Sprite" in (name_el.text or ""):
                    sprite_decals.append(item)
        assert len(sprite_decals) == 0, "Atlas sprites should use SurfaceGui, not Decal"

    def test_sprite_without_rect_uses_decal(self, tmp_path):
        """When sprite has no rect attributes, should use Decal (full texture)."""
        from roblox.rbxlx_writer import write_rbxlx
        part = RbxPart(
            name="FullSprite",
            size=(2.0, 0.1, 2.0),
        )
        part.attributes["_SpriteTextureId"] = "rbxassetid://12345"
        # No _SpriteRect* attributes

        place = RbxPlace(workspace_parts=[part])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)

        tree = ET.parse(output)
        root = tree.getroot()

        # Should have Decal children
        decals = []
        for item in root.iter("Item"):
            if item.get("class") == "Decal":
                name_el = item.find("Properties/string[@name='Name']")
                if name_el is not None and "SpriteDecal" in (name_el.text or ""):
                    decals.append(item)
        assert len(decals) == 2, f"Expected 2 SpriteDecals (Front+Back), got {len(decals)}"

        # Should NOT have SpriteSurfaceGuis
        surface_guis = []
        for item in root.iter("Item"):
            if item.get("class") == "SurfaceGui":
                name_el = item.find("Properties/string[@name='Name']")
                if name_el is not None and "SpriteSurfaceGui" in (name_el.text or ""):
                    surface_guis.append(item)
        assert len(surface_guis) == 0
