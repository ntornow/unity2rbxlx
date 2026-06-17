"""Roster-emit tests for the headless luau place builder (Phase 1).

AC5 (headless surface: AddTag + SetAttribute per member), AC13 (reserved-RS-name
for the container), single-AddTag-site invariant, and the AC7 cross-emitter
parity test (parse BOTH artifacts and assert the decoded tag-sets are equal).
"""

import base64 as _b64
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.roblox_types import RbxPlace, RbxScript  # noqa: E402
from roblox.luau_place_builder import generate_place_luau  # noqa: E402
from roblox.rbxlx_writer import write_rbxlx  # noqa: E402
from converter.roster_assembly import DEFAULT_ROSTER_CONTAINER  # noqa: E402
from tests._roster_fixtures import (  # noqa: E402
    CHAR_NAME, CHILD_SCRIPT_NAME, CHILD_SCRIPT_SRC, LABEL, ROOT_SCRIPT_NAME,
    ROOT_SCRIPT_SRC, TEMPLATE_NAME, UNITY_TAG, make_place_with_roster,
)
from converter.roster_assembly import strip_member_scripts  # noqa: E402
from tests._roster_fixtures import make_model_rooted_template  # noqa: E402


def _addtag_calls(luau):
    """All CollectionService:AddTag(var, "tag") tag strings in the script."""
    return re.findall(r'CS:AddTag\([^,]+,"([^"]+)"\)', luau)


def test_ac5_emits_addtag_and_setattribute(tmp_path):
    place = make_place_with_roster()
    luau = generate_place_luau(place)
    # CollectionService hoisted.
    assert "local CS=game:GetService('CollectionService')" in luau
    # Container Folder built and parented to RS.
    assert f"RC.Name=\"{DEFAULT_ROSTER_CONTAINER}\"" in luau
    assert "RC.Parent=RS" in luau
    # AddTag with the label (root only).
    tags = _addtag_calls(luau)
    assert tags == [LABEL], tags
    # characterName set via SetAttribute on the member.
    assert f'SetAttribute("characterName","{CHAR_NAME}")' in luau


def test_single_addtag_site_no_marker_setattribute(tmp_path):
    place = make_place_with_roster()
    luau = generate_place_luau(place)
    # Exactly one AddTag per member (single-site invariant).
    assert luau.count("CS:AddTag(") == 1
    # The _RosterTag marker is NEVER emitted as a SetAttribute on luau.
    assert "_RosterTag" not in luau


def test_ac13_luau_container_reserved_disambiguated(tmp_path):
    place = make_place_with_roster()
    place.scripts.append(RbxScript(
        name=DEFAULT_ROSTER_CONTAINER, source="return {}",
        script_type="ModuleScript",
    ))
    luau = generate_place_luau(place)
    # Container disambiguated; no same-named RemoteEvent.
    assert f'RC.Name="{DEFAULT_ROSTER_CONTAINER}_1"' in luau
    assert f're.Name="{DEFAULT_ROSTER_CONTAINER}_1"' not in luau
    assert f're.Name="{DEFAULT_ROSTER_CONTAINER}"' not in luau


def test_no_roster_no_container(tmp_path):
    luau = generate_place_luau(RbxPlace())
    assert "RC.Name=" not in luau
    assert "CS:AddTag(" not in luau


# --- AC7 — cross-emitter parity, parse BOTH artifacts ------------------------

def _decode_rbxlx_member_tags(rbxlx_path):
    """Parse the rbxlx, find the roster member's Tags BinaryString, decode it."""
    root = ET.parse(rbxlx_path).getroot()
    rs = next(it for it in root.iter("Item")
              if it.get("class") == "ReplicatedStorage")
    container = None
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
            container = item
    assert container is not None
    member = container.findall("Item")[0]
    props = member.find("Properties")
    tags_elem = next(b for b in props.findall("BinaryString")
                     if b.get("name") == "Tags")
    raw = _b64.b64decode(tags_elem.text)
    return set(raw.decode("utf-8").split("\0")) if raw else set()


def test_ac7_cross_emitter_tag_parity(tmp_path):
    """Drive BOTH writers off one RbxPlace; the decoded rbxlx tag-set MUST
    equal the luau AddTag tag-set (computed from the actual artifacts)."""
    place = make_place_with_roster(with_unity_tag=True)

    rbxlx_path = tmp_path / "parity.rbxlx"
    write_rbxlx(place, rbxlx_path)
    rbxlx_tags = _decode_rbxlx_member_tags(rbxlx_path)

    luau = generate_place_luau(place)
    luau_tags = set(_addtag_calls(luau))

    # Both writers union {Unity m_TagString} ∪ {roster label}; the decoded
    # sets MUST be EQUAL (computed from the actual artifacts on both sides).
    assert rbxlx_tags == luau_tags == {UNITY_TAG, LABEL}


# --- Script-strip hardening — roster copies carry NO template scripts --------

def _container_member_item(rbxlx_path):
    """Parse the rbxlx and return the first roster-member Item under the
    dedicated container Folder in ReplicatedStorage."""
    root = ET.parse(rbxlx_path).getroot()
    rs = next(it for it in root.iter("Item")
              if it.get("class") == "ReplicatedStorage")
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
            return item.findall("Item")[0]
    raise AssertionError("no roster container/member found")


def _templates_item(rbxlx_path, name):
    """Return the named template Item under ReplicatedStorage.Templates."""
    root = ET.parse(rbxlx_path).getroot()
    rs = next(it for it in root.iter("Item")
              if it.get("class") == "ReplicatedStorage")
    templates = None
    for item in rs.findall("Item"):
        if item.get("class") != "Folder":
            continue
        props = item.find("Properties")
        nm = None
        if props is not None:
            for s in props.findall("string"):
                if s.get("name") == "Name":
                    nm = s.text
        if nm == "Templates":
            templates = item
    assert templates is not None
    for item in templates.findall("Item"):
        props = item.find("Properties")
        if props is None:
            continue
        for s in props.findall("string"):
            if s.get("name") == "Name" and s.text == name:
                return item
    raise AssertionError(f"template {name} not found")


_SCRIPT_CLASSES = {"Script", "LocalScript", "ModuleScript"}


def _descendant_script_names(item):
    """All Script/LocalScript/ModuleScript Item names anywhere under *item*."""
    return [it.get("class") for it in item.iter("Item")
            if it.get("class") in _SCRIPT_CLASSES]


def test_strip_member_scripts_pure_helper():
    """strip_member_scripts empties scripts on root AND descendants of a copy."""
    import copy as _copy
    tmpl = make_model_rooted_template(with_scripts=True)
    member = _copy.deepcopy(tmpl)
    strip_member_scripts(member)
    assert member.scripts == []
    for child in member.children:
        assert child.scripts == []
    # The source template (a separate object) is UNTOUCHED.
    assert [s.name for s in tmpl.scripts] == [ROOT_SCRIPT_NAME]
    assert [s.name for c in tmpl.children for s in c.scripts] == [CHILD_SCRIPT_NAME]


def test_rbxlx_roster_copy_has_no_scripts(tmp_path):
    """The rbxlx roster member copy carries NONE of the template's scripts,
    while the canonical Templates child still ships them."""
    place = make_place_with_roster(with_scripts=True)
    rbxlx_path = tmp_path / "scripts.rbxlx"
    write_rbxlx(place, rbxlx_path)

    member = _container_member_item(rbxlx_path)
    assert _descendant_script_names(member) == [], \
        "roster copy must carry no Script/LocalScript/ModuleScript children"

    # The canonical Templates child is untouched — it still ships both scripts.
    tmpl_item = _templates_item(rbxlx_path, TEMPLATE_NAME)
    assert sorted(_descendant_script_names(tmpl_item)) == ["Script", "Script"]
    # And the script SOURCE only appears once (under Templates), not duplicated
    # under the roster container.
    raw = rbxlx_path.read_text()
    assert raw.count(ROOT_SCRIPT_SRC) == 1
    assert raw.count(CHILD_SCRIPT_SRC) == 1


def test_luau_roster_copy_has_no_scripts(tmp_path):
    """The luau roster member build emits NO inline script for the copy; the
    template script source appears only via the Templates emit, not N times."""
    place = make_place_with_roster(with_scripts=True)
    luau = generate_place_luau(place)
    # Each template script source appears exactly once (the Templates emit),
    # never re-emitted under the roster container.
    assert luau.count(ROOT_SCRIPT_SRC.splitlines()[0]) == 1
    assert luau.count(CHILD_SCRIPT_SRC.splitlines()[0]) == 1


def test_ac7_label_only_parity(tmp_path):
    """Without a Unity tag, the decoded rbxlx tag-set EQUALS the luau set."""
    place = make_place_with_roster(with_unity_tag=False)
    rbxlx_path = tmp_path / "parity2.rbxlx"
    write_rbxlx(place, rbxlx_path)
    rbxlx_tags = _decode_rbxlx_member_tags(rbxlx_path)
    luau = generate_place_luau(place)
    luau_tags = set(_addtag_calls(luau))
    assert rbxlx_tags == luau_tags == {LABEL}
