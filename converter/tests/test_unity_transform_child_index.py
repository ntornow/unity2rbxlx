"""Tests for the unity_transform_child_index coherence pack.

Unity transform.GetChild(n) indexes only child GameObjects; the transpiler
emits raw ``GetChildren()[n]`` which (in Roblox) includes injected Sounds/
Scripts and grabs the wrong instance (the Turret HitSound.CFrame crash). The
pack rewrites that to a _SceneRuntimeId-filtered helper.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence_packs import run_packs

PACK = {"unity_transform_child_index"}


def _run(source: str) -> RbxScript:
    s = RbxScript(name="Turret", source=source, script_type="Script")
    run_packs([s], enabled=PACK)
    return s


class TestRewrite:
    def test_simple_index_rewritten_and_helper_injected(self):
        s = _run("local b = container:GetChildren()[1]\n")
        assert "__unityChild(container, 1)" in s.source
        assert "container:GetChildren()[1]" not in s.source
        assert "local function __unityChild(" in s.source
        assert '_SceneRuntimeId' in s.source  # filters on the host attribute

    def test_all_call_sites_rewritten_single_helper(self):
        src = (
            "local function tBase() return container:GetChildren()[1] end\n"
            "local function tWeapon() local b = tBase(); return b and b:GetChildren()[1] end\n"
            "local function tOrigin() local w = tWeapon(); return w and w:GetChildren()[1] end\n"
        )
        s = _run(src)
        assert "__unityChild(container, 1)" in s.source
        assert "b and __unityChild(b, 1)" in s.source
        assert "w and __unityChild(w, 1)" in s.source
        assert "GetChildren()[1]" not in s.source
        # helper injected exactly once
        assert s.source.count("local function __unityChild(") == 1

    def test_higher_index_preserved(self):
        s = _run("local x = node:GetChildren()[3]\n")
        assert "__unityChild(node, 3)" in s.source


class TestNoFalsePositives:
    def test_plain_getchildren_loop_untouched(self):
        # Iterating all children (no literal index) is a legitimate all-children
        # walk and must NOT be rewritten.
        src = "for _, c in ipairs(parent:GetChildren()) do print(c) end\n"
        s = _run(src)
        assert s.source == src
        assert "__unityChild" not in s.source

    def test_unrelated_script_is_noop(self):
        src = "local x = 1 + 2\n"
        s = _run(src)
        assert s.source == src


class TestIdempotency:
    def test_running_twice_equals_once(self):
        src = "local b = container:GetChildren()[1]\n"
        s = RbxScript(name="Turret", source=src, script_type="Script")
        run_packs([s], enabled=PACK)
        once = s.source
        run_packs([s], enabled=PACK)  # second pass must be a no-op
        assert s.source == once
        assert s.source.count("local function __unityChild(") == 1
