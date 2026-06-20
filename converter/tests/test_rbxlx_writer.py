"""
test_rbxlx_writer.py -- Tests for RBXLX file generation.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import (
    RbxPart, RbxCFrame, RbxScript, RbxPlace, RbxLightingConfig,
    RbxScreenGui, RbxUIElement, RbxPostProcessing,
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

        # Find the TestPart and verify its properties are actually written
        test_part = None
        for item in root.iter("Item"):
            if item.get("class") == "Part":
                name_prop = item.find(".//string[@name='Name']")
                if name_prop is not None and name_prop.text == "TestPart":
                    test_part = item
                    break
        assert test_part is not None, "TestPart not found in XML"
        props = test_part.find("Properties")
        assert props is not None, "TestPart has no Properties element"

        # CFrame must be present with correct position
        cframe = props.find("CoordinateFrame[@name='CFrame']")
        assert cframe is not None, "TestPart missing CFrame property"
        assert cframe.find("X").text == "1.0" or cframe.find("X").text == "1"

        # Size must be present
        size = props.find("Vector3[@name='Size']")
        assert size is not None, "TestPart missing Size property"
        assert float(size.find("X").text) == 4.0

        # Anchored must be present
        anchored = props.find("bool[@name='Anchored']")
        assert anchored is not None, "TestPart missing Anchored property"

        # Color must be present
        color = props.find("Color3uint8[@name='Color3uint8']")
        assert color is not None, "TestPart missing Color3uint8 property"

        # Smooth surfaces
        top = props.find("token[@name='TopSurface']")
        assert top is not None, "TestPart missing TopSurface property"

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

    def test_post_processing_attributes_serialize(self, tmp_path):
        """Post-processing extra attributes must serialize. The writer called an
        undefined ``_write_attributes`` here — any truthy ``pp.attributes`` raised
        NameError at this point. Guards both the helper and that reachable path."""
        from roblox.rbxlx_writer import write_rbxlx
        pp = RbxPostProcessing(
            attributes={"VignetteIntensity": 0.5, "MotionBlur": True, "Profile": "urp"}
        )
        place = RbxPlace(post_processing=pp)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)  # pre-fix: NameError: _write_attributes

        content = output.read_text()
        assert 'name="AttributesSerialize"' in content

    def test_empty_post_processing_attributes_emit_nothing(self, tmp_path):
        """Empty attributes dict must not emit an AttributesSerialize element."""
        from roblox.rbxlx_writer import write_rbxlx
        place = RbxPlace(post_processing=RbxPostProcessing())
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        assert 'name="AttributesSerialize"' not in output.read_text()

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


class TestAutoRemoteEventReservedNames:
    """The auto-RemoteEvent generator scans scripts for ``WaitForChild("X")``
    and creates a same-named RemoteEvent. It must NOT create one for a
    name that another writer path will also add as a Folder or
    ModuleScript — otherwise ReplicatedStorage ends up with two siblings
    named X and ``WaitForChild("X")`` returns a non-deterministic match.

    Reproducer: SimpleFPS Player.luau does
    ``ReplicatedStorage.Templates:WaitForChild("Rifle")``. The "Templates"
    string in the lookup made the auto-generator emit a RemoteEvent named
    "Templates" alongside the Folder named "Templates" that the prefab
    packages writer creates — turret scripts then resolved Templates to
    the RemoteEvent (no children) and infinite-yielded on
    ``Templates:WaitForChild("TurretBullet")``.
    """

    def _place_with_template_lookup(self, has_replicated_templates: bool):
        """Build a place with a script that WaitForChild's "Templates"
        AND uses a RemoteEvent API (FireServer / OnServerEvent) on a
        different name. The latter is what makes the heuristic decide
        the lookup might be a RemoteEvent.
        """
        script = RbxScript(
            name="Player",
            source=(
                'local ReplicatedStorage = game:GetService("ReplicatedStorage")\n'
                'local templates = ReplicatedStorage:WaitForChild("Templates")\n'
                'local fireRemote = ReplicatedStorage:WaitForChild("FireRemote")\n'
                "fireRemote:FireServer()\n"
                'templates:WaitForChild("Rifle")\n'
            ),
            script_type="LocalScript",
        )
        place = RbxPlace()
        place.scripts.append(script)
        if has_replicated_templates:
            # Anything in this list triggers the writer to create the
            # Templates Folder.
            place.replicated_templates.append(
                RbxPart(name="Rifle", class_name="Model")
            )
        return place

    def _count_replicated_storage_children_named(self, root, name: str) -> dict[str, int]:
        """Walk the rbxlx XML and return {ClassName: count} for direct
        ReplicatedStorage children whose Name matches ``name``.
        """
        out: dict[str, int] = {}
        for item in root.iter("Item"):
            cls = item.get("class")
            if cls != "ReplicatedStorage":
                continue
            # Direct children: those Items whose immediate Item parent is
            # the ReplicatedStorage Item we just found.
            for child in item.findall("Item"):
                child_cls = child.get("class") or ""
                name_el = child.find("Properties/string[@name='Name']")
                if name_el is not None and (name_el.text or "") == name:
                    out[child_cls] = out.get(child_cls, 0) + 1
        return out

    def test_no_remoteevent_named_templates_when_folder_exists(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = self._place_with_template_lookup(has_replicated_templates=True)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        tree = ET.parse(output)
        root = tree.getroot()
        children = self._count_replicated_storage_children_named(root, "Templates")
        assert children.get("Folder", 0) == 1, (
            "Templates Folder must exist (replicated_templates is non-empty)"
        )
        assert "RemoteEvent" not in children, (
            "Auto-RemoteEvent generator must skip 'Templates' when the "
            "writer is also adding a Folder by that name. Got: %r" % children
        )

    def test_remoteevent_emitted_when_no_folder_collision(self, tmp_path):
        # Sanity-check: the heuristic still emits RemoteEvents for names
        # that AREN'T reserved. Otherwise the bug fix could over-suppress.
        from roblox.rbxlx_writer import write_rbxlx
        place = self._place_with_template_lookup(has_replicated_templates=False)
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        tree = ET.parse(output)
        root = tree.getroot()
        # FireRemote was looked up + used with FireServer — should be
        # auto-created as a RemoteEvent.
        fireremote = self._count_replicated_storage_children_named(root, "FireRemote")
        assert fireremote.get("RemoteEvent", 0) == 1, (
            "auto-RemoteEvent for FireRemote must still be emitted; got %r"
            % fireremote
        )

    def test_no_remoteevent_named_after_modulescript(self, tmp_path):
        # If any script is a ModuleScript with name "EventSystem", and a
        # different script does ``ReplicatedStorage:WaitForChild("EventSystem")``,
        # we must NOT create a RemoteEvent named "EventSystem" — the
        # ModuleScript will be added to ReplicatedStorage by the storage
        # classifier and a sibling RemoteEvent of the same name would
        # cause the same WaitForChild ambiguity.
        from roblox.rbxlx_writer import write_rbxlx
        module = RbxScript(
            name="EventSystem",
            source="local M = {}\nfunction M.fire() end\nreturn M\n",
            script_type="ModuleScript",
        )
        consumer = RbxScript(
            name="Other",
            source=(
                'local ReplicatedStorage = game:GetService("ReplicatedStorage")\n'
                'local es = ReplicatedStorage:WaitForChild("EventSystem")\n'
                'local r = ReplicatedStorage:WaitForChild("Hit")\n'
                "r:FireServer()\n"
            ),
            script_type="LocalScript",
        )
        place = RbxPlace()
        place.scripts.extend([module, consumer])
        output = tmp_path / "test.rbxlx"
        write_rbxlx(place, output)
        tree = ET.parse(output)
        root = tree.getroot()
        children = self._count_replicated_storage_children_named(root, "EventSystem")
        # The storage classifier puts ModuleScripts in ReplicatedStorage by
        # default. We don't want a sibling RemoteEvent of the same name.
        assert "RemoteEvent" not in children, (
            "Auto-RemoteEvent generator must skip names that match an "
            "emitted ModuleScript. Got: %r" % children
        )


# ---------------------------------------------------------------------------
# Addressables Unit-4 roster surface (Phase 1) — AC4/AC7/AC11/AC12/AC13
# ---------------------------------------------------------------------------

import base64 as _b64
import struct as _struct

from tests._roster_fixtures import (  # noqa: E402
    CHAR_NAME, LABEL, TEMPLATE_NAME, UNITY_TAG,
    make_place_with_roster,
)
from converter.roster_assembly import (  # noqa: E402
    DEFAULT_ROSTER_CONTAINER, ROSTER_TAG_MARKER,
)


def _decode_tags(binstr_text):
    """Decode a Roblox Tags BinaryString: base64 -> split on NUL."""
    raw = _b64.b64decode(binstr_text)
    return set(raw.decode("utf-8").split("\0")) if raw else set()


def _decode_attributes(binstr_text):
    """Decode an AttributesSerialize blob into {key: value} (str/num/bool)."""
    raw = _b64.b64decode(binstr_text)
    out = {}
    off = 0
    (count,) = _struct.unpack_from("<I", raw, off); off += 4
    for _ in range(count):
        (klen,) = _struct.unpack_from("<I", raw, off); off += 4
        key = raw[off:off + klen].decode("utf-8"); off += klen
        tid = raw[off]; off += 1
        if tid == 0x02:  # String
            (vlen,) = _struct.unpack_from("<I", raw, off); off += 4
            val = raw[off:off + vlen].decode("utf-8"); off += vlen
        elif tid == 0x03:  # Bool
            val = bool(raw[off]); off += 1
        elif tid == 0x06:  # Float64
            (val,) = _struct.unpack_from("<d", raw, off); off += 8
        else:
            raise AssertionError(f"unexpected attr type {tid}")
        out[key] = val
    return out


def _find_rs(root):
    for item in root.iter("Item"):
        if item.get("class") == "ReplicatedStorage":
            return item
    return None


def _rs_named_children(root, name):
    """Direct ReplicatedStorage Item children whose Name property == name,
    returned as {class_name: count}."""
    rs = _find_rs(root)
    assert rs is not None
    out = {}
    for item in rs.findall("Item"):
        nm = None
        props = item.find("Properties")
        if props is not None:
            for s in props.findall("string"):
                if s.get("name") == "Name":
                    nm = s.text
        if nm == name:
            out[item.get("class")] = out.get(item.get("class"), 0) + 1
    return out


def _container_folder(root):
    """Return the roster-container Folder Item under ReplicatedStorage."""
    rs = _find_rs(root)
    assert rs is not None
    for item in rs.findall("Item"):
        if item.get("class") != "Folder":
            continue
        props = item.find("Properties")
        nm = None
        if props is not None:
            for s in props.findall("string"):
                if s.get("name") == "Name":
                    nm = s.text
        if nm and nm.startswith(DEFAULT_ROSTER_CONTAINER):
            return item, nm
    return None, None


def _member_roots(container):
    """Direct member-root Items inside the container (its immediate children)."""
    return container.findall("Item")


def _props_binstrings(item):
    """Map {name: text} of BinaryString props on an Item."""
    out = {}
    props = item.find("Properties")
    if props is not None:
        for b in props.findall("BinaryString"):
            out[b.get("name")] = b.text
    return out


class TestRosterEmit:
    def test_ac4_model_rooted_member_emits_decodable_tags(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = make_place_with_roster()
        out = tmp_path / "roster.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        container, cname = _container_folder(root)
        assert container is not None, "roster container Folder missing"
        members = _member_roots(container)
        assert len(members) == 1
        member = members[0]
        # The member root is a Model (Model-rooted multi-part case).
        assert member.get("class") == "Model"
        bins = _props_binstrings(member)
        assert "Tags" in bins, "Model-rooted member must emit a Tags element"
        decoded = _decode_tags(bins["Tags"])
        assert LABEL in decoded
        # Attributes decode to characterName, with NO _RosterTag marker.
        attrs = _decode_attributes(bins["AttributesSerialize"])
        assert attrs.get("characterName") == CHAR_NAME
        assert ROSTER_TAG_MARKER not in attrs

    def test_ac4_descendants_not_tagged(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = make_place_with_roster()
        out = tmp_path / "roster.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        container, _ = _container_folder(root)
        member = _member_roots(container)[0]
        # Descendant Parts of the member must NOT carry the label tag.
        for sub in member.findall("Item"):
            bins = _props_binstrings(sub)
            if "Tags" in bins:
                assert LABEL not in _decode_tags(bins["Tags"])

    def test_ac11_single_tags_element_with_union(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        # Root ALSO carries a Unity tag → union {Unity tag} ∪ {label}, one elem.
        place = make_place_with_roster(with_unity_tag=True)
        out = tmp_path / "roster.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        container, _ = _container_folder(root)
        member = _member_roots(container)[0]
        props = member.find("Properties")
        tags_elems = [b for b in props.findall("BinaryString")
                      if b.get("name") == "Tags"]
        assert len(tags_elems) == 1, "exactly one Tags element per member"
        decoded = _decode_tags(tags_elems[0].text)
        assert decoded == {UNITY_TAG, LABEL}

    def test_ac12_referent_uniqueness_and_intra_member_part1(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = make_place_with_roster()
        out = tmp_path / "roster.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        # (a) every referent in the whole document is unique
        referents = [it.get("referent") for it in root.iter("Item")
                     if it.get("referent")]
        assert len(referents) == len(set(referents)), "duplicate referents!"
        # (b) the member's constraint Part1 resolves to a part INSIDE the copy
        container, _ = _container_folder(root)
        member = _member_roots(container)[0]
        member_referents = {it.get("referent") for it in member.iter("Item")
                            if it.get("referent")}
        part1_refs = []
        for ref in member.iter("Ref"):
            if ref.get("name") == "Part1" and ref.text:
                part1_refs.append(ref.text)
        assert part1_refs, "member constraint should have a Part1"
        for r in part1_refs:
            assert r in member_referents, "Part1 must resolve within the copy"
        # (c) the source template's referents are disjoint from the copy's
        # (no aliasing) — find the Templates folder member.
        rs = _find_rs(root)
        templates = None
        for item in rs.findall("Item"):
            if item.get("class") == "Folder":
                props = item.find("Properties")
                nm = None
                if props is not None:
                    for s in props.findall("string"):
                        if s.get("name") == "Name":
                            nm = s.text
                if nm == "Templates":
                    templates = item
        assert templates is not None
        tmpl_referents = {it.get("referent") for it in templates.iter("Item")
                          if it.get("referent")}
        assert member_referents.isdisjoint(tmpl_referents)

    def test_ac13_container_reserved_name_disambiguated(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        from core.roblox_types import RbxScript
        place = make_place_with_roster()
        # Occupy the default container name with a ModuleScript so the writer
        # must suffix-disambiguate AND reserve the chosen name.
        place.scripts.append(RbxScript(
            name=DEFAULT_ROSTER_CONTAINER,
            source="return {}",
            script_type="ModuleScript",
        ))
        out = tmp_path / "roster.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        container, cname = _container_folder(root)
        assert container is not None
        assert cname != DEFAULT_ROSTER_CONTAINER  # disambiguated
        assert cname.startswith(DEFAULT_ROSTER_CONTAINER + "_")
        # No RemoteEvent of the chosen container name was minted.
        siblings = _rs_named_children(root, cname)
        assert "RemoteEvent" not in siblings
        assert siblings.get("Folder", 0) == 1

    def test_no_roster_no_container(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        place = RbxPlace()
        out = tmp_path / "empty.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        container, _ = _container_folder(root)
        assert container is None

    def test_canonical_template_unchanged(self, tmp_path):
        """The source template's attributes must not gain the roster marker."""
        place = make_place_with_roster()
        before = dict(place.replicated_templates[0].attributes)
        from roblox.rbxlx_writer import write_rbxlx
        write_rbxlx(place, tmp_path / "roster.rbxlx")
        after = dict(place.replicated_templates[0].attributes)
        assert ROSTER_TAG_MARKER not in after
        assert before == after


class TestScreenGuiEnabled:
    """ScreenGui `Enabled` static contract serialization. (AC#3, AC#5)"""

    @staticmethod
    def _screen_gui_props(output: Path) -> ET.Element:
        """Return the <Properties> element of the (single) ScreenGui item."""
        root = ET.parse(output).getroot()
        for item in root.iter("Item"):
            if item.get("class") == "ScreenGui":
                props = item.find("Properties")
                assert props is not None
                return props
        raise AssertionError("no ScreenGui Item found")

    def test_enabled_true_serialized(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        gui = RbxScreenGui(name="EnabledUI", enabled=True)
        out = tmp_path / "t.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[gui]), out)
        assert '<bool name="Enabled">true</bool>' in out.read_text()

    def test_enabled_false_serialized(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        gui = RbxScreenGui(name="DisabledUI", enabled=False)
        out = tmp_path / "t.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[gui]), out)
        assert '<bool name="Enabled">false</bool>' in out.read_text()

    def test_enabled_default_true_backcompat(self, tmp_path):
        """A ScreenGui built without `enabled=` serializes Enabled=true. (AC#5)"""
        from roblox.rbxlx_writer import write_rbxlx
        gui = RbxScreenGui(name="DefaultUI")
        out = tmp_path / "t.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[gui]), out)
        assert '<bool name="Enabled">true</bool>' in out.read_text()

    def test_enabled_before_attributes_serialize(self, tmp_path):
        """Enabled is emitted before AttributesSerialize. (AC#3)"""
        from roblox.rbxlx_writer import write_rbxlx
        # Attributes present so AttributesSerialize is emitted.
        gui = RbxScreenGui(name="OrderUI", enabled=False,
                           attributes={"_SceneRuntimeId": "S:1"})
        out = tmp_path / "t.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[gui]), out)
        props = self._screen_gui_props(out)
        children = list(props)
        names = [(c.tag, c.get("name")) for c in children]
        enabled_idx = next(
            i for i, (tag, nm) in enumerate(names)
            if tag == "bool" and nm == "Enabled")
        attrs_idx = next(
            i for i, (tag, nm) in enumerate(names)
            if tag == "BinaryString" and nm == "AttributesSerialize")
        assert enabled_idx < attrs_idx, names

    def test_ducktyped_without_enabled_omits_enabled(self, tmp_path):
        """A duck-typed ScreenGui lacking `enabled` does NOT emit Enabled.

        The writer guards with `hasattr(sg, "enabled")`, so a back-compat
        object without the attr emits no `name="Enabled"` (Roblox default
        true applies at load). A real RbxScreenGui DOES emit it. (AC#5)
        """
        from types import SimpleNamespace
        from roblox.rbxlx_writer import write_rbxlx
        duck = SimpleNamespace(name="DuckUI", elements=[], attributes={})
        assert not hasattr(duck, "enabled")
        out = tmp_path / "duck.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[duck]), out)
        text = out.read_text()
        assert 'name="Enabled"' not in text

        real = RbxScreenGui(name="RealUI")
        out2 = tmp_path / "real.rbxlx"
        write_rbxlx(RbxPlace(screen_guis=[real]), out2)
        assert '<bool name="Enabled">true</bool>' in out2.read_text()
