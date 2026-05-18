"""
test_health_check_injector.py -- Tests for injecting/removing smoke-test
health-check scripts into .rbxlx files.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from roblox.health_check_injector import (
    inject_health_check,
    remove_health_check,
    HEALTH_CHECK_SCRIPT_NAME,
    CLIENT_HEALTH_CHECK_SCRIPT_NAME,
    _find_service,
    _create_service,
    _find_child_item,
    _find_or_create_starter_player_scripts,
    _remove_existing_health_check,
)

MINIMAL_RBXLX = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<roblox version="4">\n'
    "</roblox>\n"
)


def _write_rbxlx(tmp_path, body=MINIMAL_RBXLX, name="place.rbxlx"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _named_items(parent, name):
    """Direct child Items whose Name property equals `name`."""
    found = []
    for item in parent:
        if item.tag != "Item":
            continue
        props = item.find("Properties")
        if props is None:
            continue
        name_elem = props.find("string[@name='Name']")
        if name_elem is not None and name_elem.text == name:
            found.append(item)
    return found


class TestFindService:
    def test_find_by_class_attribute(self):
        root = ET.fromstring(
            '<roblox><Item class="Workspace"></Item></roblox>'
        )
        svc = _find_service(root, "Workspace")
        assert svc is not None
        assert svc.get("class") == "Workspace"

    def test_find_by_name_property(self):
        root = ET.fromstring(
            '<roblox><Item class="Service">'
            '<Properties><string name="Name">Lighting</string></Properties>'
            "</Item></roblox>"
        )
        assert _find_service(root, "Lighting") is not None

    def test_returns_none_when_absent(self):
        root = ET.fromstring("<roblox></roblox>")
        assert _find_service(root, "ServerScriptService") is None


class TestCreateService:
    def test_creates_service_with_name(self):
        root = ET.fromstring("<roblox></roblox>")
        item = _create_service(root, "ServerScriptService")
        assert item.get("class") == "ServerScriptService"
        # Name property is set.
        name_elem = item.find("Properties/string[@name='Name']")
        assert name_elem is not None
        assert name_elem.text == "ServerScriptService"
        # And it is now discoverable.
        assert _find_service(root, "ServerScriptService") is not None


class TestFindOrCreateStarterPlayerScripts:
    def test_creates_starter_player_and_scripts_when_missing(self):
        root = ET.fromstring("<roblox></roblox>")
        sps = _find_or_create_starter_player_scripts(root)
        assert sps.get("class") == "StarterPlayerScripts"
        sp = _find_service(root, "StarterPlayer")
        assert sp is not None
        assert _find_child_item(sp, "StarterPlayerScripts") is sps

    def test_reuses_existing_starter_player(self):
        root = ET.fromstring(
            '<roblox><Item class="StarterPlayer"></Item></roblox>'
        )
        sps = _find_or_create_starter_player_scripts(root)
        # Only one StarterPlayer should exist.
        starters = [i for i in root if i.get("class") == "StarterPlayer"]
        assert len(starters) == 1
        assert _find_child_item(starters[0], "StarterPlayerScripts") is sps


class TestRemoveExistingHealthCheck:
    def test_removes_matching_item_and_reports_true(self):
        root = ET.fromstring("<roblox></roblox>")
        sss = _create_service(root, "ServerScriptService")
        # Manually attach a fake health-check script.
        item = ET.SubElement(sss, "Item")
        item.set("class", "Script")
        props = ET.SubElement(item, "Properties")
        ne = ET.SubElement(props, "string")
        ne.set("name", "Name")
        ne.text = HEALTH_CHECK_SCRIPT_NAME
        assert _remove_existing_health_check(sss, HEALTH_CHECK_SCRIPT_NAME) is True
        assert _named_items(sss, HEALTH_CHECK_SCRIPT_NAME) == []

    def test_no_match_reports_false(self):
        root = ET.fromstring("<roblox></roblox>")
        sss = _create_service(root, "ServerScriptService")
        assert _remove_existing_health_check(sss, HEALTH_CHECK_SCRIPT_NAME) is False


class TestInjectHealthCheck:
    def test_inject_creates_services_from_empty_place(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        out = inject_health_check(rbxlx)
        assert out.exists()
        assert out.name == "place_smoketest.rbxlx"

        root = ET.parse(out).getroot()
        sss = _find_service(root, "ServerScriptService")
        assert sss is not None
        assert len(_named_items(sss, HEALTH_CHECK_SCRIPT_NAME)) == 1

        sp = _find_service(root, "StarterPlayer")
        sps = _find_child_item(sp, "StarterPlayerScripts")
        assert sps is not None
        assert len(_named_items(sps, CLIENT_HEALTH_CHECK_SCRIPT_NAME)) == 1

    def test_inject_respects_explicit_output_path(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        dest = tmp_path / "custom_out.rbxlx"
        out = inject_health_check(rbxlx, dest)
        assert out == dest
        assert dest.exists()

    def test_injected_server_script_has_run_context_token(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        out = inject_health_check(rbxlx)
        root = ET.parse(out).getroot()
        sss = _find_service(root, "ServerScriptService")
        script = _named_items(sss, HEALTH_CHECK_SCRIPT_NAME)[0]
        token = script.find("Properties/token[@name='RunContext']")
        assert token is not None
        assert token.text == "1"

    def test_injected_source_wrapped_in_cdata(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        out = inject_health_check(rbxlx)
        raw = out.read_text(encoding="utf-8")
        assert "<![CDATA[" in raw
        # The "<=" in the Luau source must survive un-escaped inside CDATA.
        assert "#errors <= 20" in raw

    def test_inject_is_idempotent(self, tmp_path):
        # Injecting onto an already-injected place must not duplicate scripts.
        rbxlx = _write_rbxlx(tmp_path)
        first = inject_health_check(rbxlx)
        second = inject_health_check(first, first)
        root = ET.parse(second).getroot()
        sss = _find_service(root, "ServerScriptService")
        assert len(_named_items(sss, HEALTH_CHECK_SCRIPT_NAME)) == 1
        sp = _find_service(root, "StarterPlayer")
        sps = _find_child_item(sp, "StarterPlayerScripts")
        assert len(_named_items(sps, CLIENT_HEALTH_CHECK_SCRIPT_NAME)) == 1


class TestInjectExistingServices:
    def test_inject_reuses_existing_server_script_service(self, tmp_path):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<roblox version="4">'
            '<Item class="ServerScriptService"></Item>'
            "</roblox>\n"
        )
        rbxlx = _write_rbxlx(tmp_path, body)
        out = inject_health_check(rbxlx)
        root = ET.parse(out).getroot()
        # Still exactly one ServerScriptService.
        sss_items = [i for i in root if i.get("class") == "ServerScriptService"]
        assert len(sss_items) == 1
        assert len(_named_items(sss_items[0], HEALTH_CHECK_SCRIPT_NAME)) == 1


class TestRemoveHealthCheck:
    def test_remove_from_uninjected_place_returns_false(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        assert remove_health_check(rbxlx) is False

    def test_inject_then_remove_round_trip(self, tmp_path):
        rbxlx = _write_rbxlx(tmp_path)
        injected = inject_health_check(rbxlx)

        # Both scripts present after inject.
        root = ET.parse(injected).getroot()
        sss = _find_service(root, "ServerScriptService")
        assert len(_named_items(sss, HEALTH_CHECK_SCRIPT_NAME)) == 1

        # Remove operates in place and reports True.
        assert remove_health_check(injected) is True

        root_after = ET.parse(injected).getroot()
        sss_after = _find_service(root_after, "ServerScriptService")
        assert _named_items(sss_after, HEALTH_CHECK_SCRIPT_NAME) == []
        sp = _find_service(root_after, "StarterPlayer")
        sps = _find_child_item(sp, "StarterPlayerScripts")
        assert _named_items(sps, CLIENT_HEALTH_CHECK_SCRIPT_NAME) == []

        # A second remove is a no-op.
        assert remove_health_check(injected) is False
