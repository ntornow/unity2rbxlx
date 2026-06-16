"""Drive the REAL ``findOrCreateChannel`` helper (extracted from the autogen host
sources) against a fake DataModel.

Slice 1.1 r2 (P1 #1): the helper now parents each channel's BindableEvent under a
PER-MODULE ``Folder`` (``moduleFolder``) keyed on the unique module_id, with the
event named the BARE field. This proves the real Luau:
  * find-or-creates exactly ONE Folder + ONE BindableEvent (idempotent twice-call),
  * keeps two same-bare-name channels under DISTINCT folders DISTINCT,
  * resolves ``parentPath`` STRICTLY — a missing base segment returns nil and does
    NOT synthesize a junk Folder tree (fail-closed), and
  * is class-aware for the Folder layer (a same-named wrong-class sibling is not
    reused).

The host scenario harness (test_scene_runtime_host_behavior.py) MOCKS
``findOrCreateChannel``, so it cannot catch a regression in the real helper — this
file exercises the emitted source itself.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from converter.autogen import (
    _SCENE_RUNTIME_CLIENT_SOURCE,
    _SCENE_RUNTIME_SERVER_SOURCE,
)

pytestmark = pytest.mark.skipif(
    shutil.which("luau") is None, reason="luau CLI not available"
)


def _extract_find_or_create(host_source: str) -> str:
    """Slice the ``findOrCreateChannel`` function out of an autogen host source.

    The helper is a top-level ``local function`` whose body ends at the first
    column-0 ``end`` line; the next top-level construct (a comment / another
    ``local function``) follows. Slice from the marker to that column-0 ``end``,
    inclusive — robust against ``if``/``for`` keywords appearing inside comments.
    """
    marker = "local function findOrCreateChannel("
    start = host_source.index(marker)
    lines = host_source[start:].splitlines()
    out: list[str] = [lines[0]]
    for line in lines[1:]:
        out.append(line)
        if line == "end":  # column-0, terminal end of the top-level function
            break
    return "\n".join(out)


_FAKE_GAME_PREAMBLE = """\
-- Minimal fake DataModel: Instances with Name/ClassName, GetChildren,
-- FindFirstChild, IsA, and a settable Parent.
local function newInstance(className)
    local inst = {Name = className, ClassName = className, _children = {}}
    function inst:GetChildren() return self._children end
    function inst:FindFirstChild(name)
        for _, c in self._children do
            if c.Name == name then return c end
        end
        return nil
    end
    function inst:IsA(cls) return self.ClassName == cls end
    setmetatable(inst, {
        __newindex = function(t, k, v)
            if k == "Parent" then
                if v ~= nil then table.insert(v._children, t) end
                rawset(t, "_parent", v)
            else
                rawset(t, k, v)
            end
        end,
    })
    return inst
end

local _created = {count = 0}
Instance = {}
function Instance.new(className)
    _created.count = _created.count + 1
    return newInstance(className)
end

-- Build a fake ``game`` with one real container (ReplicatedStorage).
game = newInstance("DataModel")
game.Name = "game"
local RS = newInstance("ReplicatedStorage")
RS.Parent = game
"""


def _run_luau(body: str) -> tuple[int, str, str]:
    fn = _extract_find_or_create(_SCENE_RUNTIME_CLIENT_SOURCE)
    script = _FAKE_GAME_PREAMBLE + "\n" + fn + "\n" + body + "\n"
    with tempfile.NamedTemporaryFile(
        suffix=".luau", mode="w", delete=False
    ) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(
            ["luau", path], capture_output=True, text=True, timeout=15
        )
        return r.returncode, r.stdout, r.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def _code_lines(fn: str) -> list[str]:
    # Executable lines only — drop comment-only lines (the two host copies differ
    # only in their comment prose, not their logic).
    out = []
    for ln in fn.splitlines():
        s = ln.strip()
        if s and not s.startswith("--"):
            out.append(s)
    return out


def test_client_and_server_helpers_have_identical_logic():
    # Both host sources carry the same helper logic; testing the client copy
    # covers both. (Comment prose may differ; the executable lines must not.)
    assert _code_lines(
        _extract_find_or_create(_SCENE_RUNTIME_CLIENT_SOURCE)
    ) == _code_lines(_extract_find_or_create(_SCENE_RUNTIME_SERVER_SOURCE))


def test_creates_one_folder_and_event_then_idempotent():
    rc, out, err = _run_luau("""\
        local e1 = findOrCreateChannel("AmmoUpdate", "ReplicatedStorage", "sec_mod1")
        assert(e1 ~= nil, "first call must return an event")
        assert(e1.ClassName == "BindableEvent", "must be a BindableEvent")
        -- The per-module Folder exists under RS with the right name+class.
        local folder = RS:FindFirstChild("sec_mod1")
        assert(folder ~= nil and folder.ClassName == "Folder", "folder missing")
        assert(folder:FindFirstChild("AmmoUpdate") == e1, "event under folder")
        -- Idempotent: a second call returns the SAME instance, no duplicate.
        local e2 = findOrCreateChannel("AmmoUpdate", "ReplicatedStorage", "sec_mod1")
        assert(e1 == e2, "second call must return the same instance")
        assert(#folder:GetChildren() == 1, "no duplicate event")
        assert(#RS:GetChildren() == 1, "no duplicate folder")
        print("OK")
    """)
    assert rc == 0, f"{err}\n{out}"
    assert "OK" in out, out


def test_distinct_folders_keep_same_bare_name_distinct():
    # P1 #1 core: two modules' channels share the bare name ``AmmoUpdate`` but live
    # under DISTINCT per-module folders -> DISTINCT BindableEvents (no aliasing).
    rc, out, err = _run_luau("""\
        local a = findOrCreateChannel("AmmoUpdate", "ReplicatedStorage", "sec_A")
        local b = findOrCreateChannel("AmmoUpdate", "ReplicatedStorage", "sec_B")
        assert(a ~= b, "distinct folders must yield distinct events")
        assert(RS:FindFirstChild("sec_A") ~= RS:FindFirstChild("sec_B"))
        print("OK")
    """)
    assert rc == 0, f"{err}\n{out}"
    assert "OK" in out, out


def test_missing_base_container_returns_nil_no_junk_tree():
    # Strict base resolution: a parent_path whose base segment does not exist must
    # return nil and NOT synthesize a Folder (fail-closed, not a junk tree).
    rc, out, err = _run_luau("""\
        local e = findOrCreateChannel("X", "NoSuchService", "sec_mod1")
        assert(e == nil, "missing base must fail closed (nil)")
        -- game has only RS as a child; nothing new was created under it.
        assert(game:FindFirstChild("NoSuchService") == nil, "no junk container")
        assert(game:FindFirstChild("sec_mod1") == nil, "no junk folder on game")
        print("OK")
    """)
    assert rc == 0, f"{err}\n{out}"
    assert "OK" in out, out


def test_folder_lookup_is_class_aware():
    # A same-named WRONG-class sibling occupying the folder name must NOT be reused
    # (or the BindableEvent parents under the wrong node / idempotency breaks).
    rc, out, err = _run_luau("""\
        -- Plant a non-Folder named like the module folder.
        local bogus = Instance.new("BindableEvent")
        bogus.Name = "sec_mod1"
        bogus.Parent = RS
        local e = findOrCreateChannel("AmmoUpdate", "ReplicatedStorage", "sec_mod1")
        assert(e ~= nil, "must still create the channel")
        -- A real Folder of that name was created alongside the bogus sibling.
        local foundFolder = nil
        for _, c in RS:GetChildren() do
            if c.Name == "sec_mod1" and c.ClassName == "Folder" then
                foundFolder = c
            end
        end
        assert(foundFolder ~= nil, "a real Folder must be created, not the bogus")
        assert(foundFolder:FindFirstChild("AmmoUpdate") == e, "event in folder")
        print("OK")
    """)
    assert rc == 0, f"{err}\n{out}"
    assert "OK" in out, out


def test_empty_module_folder_fails_closed():
    rc, out, err = _run_luau("""\
        assert(findOrCreateChannel("X", "ReplicatedStorage", "") == nil)
        assert(findOrCreateChannel("X", "ReplicatedStorage", nil) == nil)
        print("OK")
    """)
    assert rc == 0, f"{err}\n{out}"
    assert "OK" in out, out
