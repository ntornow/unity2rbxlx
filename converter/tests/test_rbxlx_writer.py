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
        assert result["parts_written"] == 0
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
        assert result["parts_written"] == 1

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
        assert result["parts_written"] == 2

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
