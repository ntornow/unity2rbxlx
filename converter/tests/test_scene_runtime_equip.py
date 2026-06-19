"""Phase 2 (camera-mount -> player-mount equip) acceptance criteria 6-9.

Drives the NEW server-side SceneRuntime equip methods through the standalone
``luau`` interpreter over a stub service surface + mock Roblox Instances. Skips
cleanly when ``luau`` is absent.

  6. resolveEquipPrefabId maps field->prefab_id from a seeded plan.equip_prefabs,
     nil on an unknown field (D13 / Edge c).
  7. equipWeaponOnCharacter welds + parents + replicates-by-parenting: a
     WeldConstraint(Part0=RightHand, Part1=weldPart) is created, the clone is
     parented under the Character named "_EquippedWeapon", and clone BaseParts are
     CanCollide=false / Massless=true.
  8. double-equip removes the prior weapon (Edge d): two calls leave exactly ONE
     _EquippedWeapon under the Character.
  9. R6 fallback + hard no-op (Edge b): RightHand when present, "Right Arm" when
     only that exists, nil when neither -> equipWeaponOnCharacter returns nil and
     creates no weld.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

HOST_RUNTIME_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
)

pytestmark = pytest.mark.skipif(
    shutil.which("luau") is None or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + host runtime file",
)


def _harness_preamble() -> str:
    host_source = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in host_source or f"[{delim}[" in host_source:
        delim += "="
    embedded = f"[{delim}[\n{host_source}\n]{delim}]"
    return textwrap.dedent(f"""\
        local HOST_RUNTIME_SOURCE = {embedded}
        local SceneRuntime
        do
            local chunk, err = loadstring(HOST_RUNTIME_SOURCE, "scene_runtime")
            assert(chunk, "load host runtime failed: " .. tostring(err))
            SceneRuntime = chunk()
        end
    """) + _HARNESS_BODY


# Mock Roblox Instance surface: enough for the equip methods. A part / model is a
# table with Name, a ClassName, parent/children bookkeeping, and the methods the
# runtime calls (FindFirstChild / IsA / GetDescendants / Destroy / CFrame).
# ``SceneRuntime`` + the helpers below are top-level LOCALS in this chunk so the
# appended scenario code sees them (standalone luau makes ``_G`` readonly, so we
# do NOT stash anything there). ``Instance`` is a real GLOBAL because the host
# runtime chunk (loaded via loadstring) reads it as a global.
_HARNESS_BODY = r"""
-- A global frame counter advanced by the stub ``task.wait`` (the poll loop's
-- yield floor). A mock can register children that "arrive" once the counter
-- reaches a target tick (see ``mockInst._arriveChildAtTick``), modelling an R15
-- limb landing K frames after the respawn poll begins.
local _tick = 0

-- ``Instance.new`` only needs to mint a WeldConstraint for the weld core.
local _createdWelds = {}
Instance = {}
function Instance.new(className)
    local inst = {
        ClassName = className,
        _children = {},
        Parent = nil,
        Part0 = nil,
        Part1 = nil,
    }
    function inst:IsA(c) return c == self.ClassName end
    function inst:Destroy() self._destroyed = true; self.Parent = nil end
    if className == "WeldConstraint" then
        table.insert(_createdWelds, inst)
        -- Setting .Parent on a weld is a no-op for the harness.
    end
    return inst
end

-- A minimal mock instance factory. Children are tracked in an ORDERED list
-- (``_childList``) and ``FindFirstChild`` scans it by LIVE ``.Name`` (Roblox
-- resolves children by live name, and the equip path renames a clone AFTER
-- parenting), skipping destroyed entries.
local function mockInst(name, className)
    local inst = {
        Name = name,
        ClassName = className or "Part",
        _childList = {},
        _descendants = {},
        Parent = nil,
        CanCollide = true,
        Massless = false,
        -- Real prefab-field templates are emitted HIDDEN (transparency=1.0) and
        -- parts default Anchored=true. Default the mock to that pinned/hidden
        -- shape so the equip path must reset it.
        Transparency = 1,
        Anchored = true,
        CFrame = "cf0",
        -- A numeric stand-in for a BasePart.Size Vector3 so the single-BasePart
        -- scale path (``clone.Size = clone.Size * equipScale``) is assertable.
        Size = 4,
        PrimaryPart = nil,
        _pivotedTo = nil,
    }
    function inst:IsA(c)
        if c == self.ClassName then return true end
        -- A "Part"/"MeshPart" mock answers true to BasePart.
        if c == "BasePart" then
            return self.ClassName == "Part" or self.ClassName == "MeshPart"
                or self._isBasePart == true
        end
        return false
    end
    function inst:FindFirstChild(n)
        -- Promote any late-arriving child whose target tick has been reached
        -- (the bounded poll advances ``_tick`` via the stub ``task.wait``).
        if self._lateArrivals then
            for name, entry in pairs(self._lateArrivals) do
                if _tick >= entry.tick then
                    entry.part.Parent = self
                    table.insert(self._childList, entry.part)
                    self._lateArrivals[name] = nil
                end
            end
        end
        for _, child in ipairs(self._childList) do
            if not child._destroyed and child.Name == n then
                return child
            end
        end
        return nil
    end
    -- Register a child that becomes findable once ``_tick`` reaches ``atTick``.
    function inst:_arriveChildAtTick(part, atTick)
        self._lateArrivals = self._lateArrivals or {}
        self._lateArrivals[part.Name] = {part = part, tick = atTick}
    end
    function inst:GetDescendants()
        return self._descendants
    end
    -- A Model uniform-scales via ScaleTo; record the factor for assertions.
    function inst:ScaleTo(s) self._scaledTo = s end
    function inst:FindFirstChildWhichIsA(c, recurse)
        for _, d in ipairs(self._descendants) do
            if d:IsA(c) then return d end
        end
        return nil
    end
    function inst:Destroy()
        self._destroyed = true
        self.Parent = nil
    end
    -- A Model relocates as a unit via PivotTo; record the target for assertions.
    -- Model the Roblox PrimaryPart semantics so anchor-placement is testable:
    -- with a PrimaryPart set, the PrimaryPart's CFrame becomes ``cf`` exactly;
    -- with NO PrimaryPart, PivotTo uses the bounding-box pivot, so the parts'
    -- own CFrames do NOT all land on ``cf`` (the bbox center does). We emulate
    -- the no-PrimaryPart case by stamping a distinct sentinel onto each part so
    -- an assertion of ``part.CFrame == cf`` fails (as it does in real Roblox).
    function inst:PivotTo(cf)
        self._pivotedTo = cf
        if self.PrimaryPart ~= nil then
            self.PrimaryPart.CFrame = cf
        else
            for _, d in ipairs(self._descendants or {}) do
                if d:IsA("BasePart") then
                    d.CFrame = "bbox_pivot_offset"
                end
            end
        end
    end
    return inst
end

local function addChild(parent, child)
    child.Parent = parent
    table.insert(parent._childList, child)
end

-- A stub services table just for the equip methods. ``clonePrefabTemplate``
-- returns whatever ``cloneFactory`` produces (parented under ``parent``).
local function equipServices(cloneFactory)
    return {
        warn = function() end,
        -- The bounded-poll yield floor. Each call advances the global frame
        -- counter so a registered late-arrival becomes findable at its tick.
        task = {
            wait = function() _tick = _tick + 1 end,
        },
        clonePrefabTemplate = function(prefabId, parent, cframe)
            local clone = cloneFactory(prefabId)
            if clone and parent then
                clone.Parent = parent
                -- Mirror Roblox auto-registering a child on ``.Parent =``.
                if parent._childList then
                    table.insert(parent._childList, clone)
                end
            end
            return clone
        end,
    }
end

-- Each test's scenario body is appended into THIS chunk, so the locals above
-- (SceneRuntime, mockInst, addChild, equipServices) and ``createdWelds`` below
-- are in scope.
local function createdWelds() return _createdWelds end
"""


def _run_scenario(scenario_body: str) -> tuple[int, str, str]:
    script = _harness_preamble() + "\n" + scenario_body + "\n"
    with tempfile.NamedTemporaryFile(
        suffix=".luau", mode="w", delete=False,
    ) as f:
        f.write(script)
        path = f.name
    try:
        result = subprocess.run(
            ["luau", path], capture_output=True, text=True, timeout=15,
        )
        return result.returncode, result.stdout, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def _assert_ok(scenario: str) -> str:
    rc, out, err = _run_scenario(scenario)
    assert rc == 0, f"luau exited {rc}\nstdout={out}\nstderr={err}"
    assert "OK" in out, f"scenario did not print OK\nstdout={out}\nstderr={err}"
    return out


# ---------------------------------------------------------------------------
# Criterion 6 — resolveEquipPrefabId
# ---------------------------------------------------------------------------

class TestResolveEquipPrefabId:

    def test_maps_field_to_prefab_id_and_nil_on_unknown(self):
        _assert_ok(textwrap.dedent("""\
            local plan = {equip_prefabs = {riflePrefab = "prefab_rifle_abc"}}
            local engine = SceneRuntime.new({warn = function() end}, plan)
            assert(engine:resolveEquipPrefabId("riflePrefab") == "prefab_rifle_abc",
                "known field must resolve to its prefab_id")
            assert(engine:resolveEquipPrefabId("unknownField") == nil,
                "unknown field must resolve to nil")
            assert(engine:resolveEquipPrefabId("") == nil, "empty field -> nil")
            -- No equip_prefabs map at all -> nil (Edge c).
            local engine2 = SceneRuntime.new({warn = function() end}, {})
            assert(engine2:resolveEquipPrefabId("riflePrefab") == nil,
                "missing equip_prefabs map -> nil")
            print("OK")
        """))


# ---------------------------------------------------------------------------
# Criterion 7 — equipWeaponOnCharacter welds + parents + collide off
# ---------------------------------------------------------------------------

class TestEquipWeaponOnCharacter:

    def test_welds_parents_and_disables_collision(self):
        _assert_ok(textwrap.dedent("""\

            -- Character with an R15 RightHand BasePart.
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            rightHand.CFrame = "handCF"
            addChild(character, rightHand)

            -- The clone is a Model with one nested BasePart (the muzzle).
            local muzzle = mockInst("Muzzle", "Part")
            local function cloneFactory(prefabId)
                local m = mockInst("RiflePrefabClone", "Model")
                m.PrimaryPart = muzzle
                m._descendants = {muzzle}
                return m
            end

            local services = equipServices(cloneFactory)
            local plan = {equip_prefabs = {riflePrefab = "prefab_rifle"}}
            local engine = SceneRuntime.new(services, plan)

            local clone = engine:equipWeaponOnCharacter(character, "prefab_rifle")
            assert(clone ~= nil, "equip must return the clone")
            -- (ii) parented under the Character + named _EquippedWeapon.
            assert(clone.Name == "_EquippedWeapon", "clone renamed to _EquippedWeapon")
            assert(clone.Parent == character, "clone parented under the Character")
            assert(character:FindFirstChild("_EquippedWeapon") == clone,
                "Character has the _EquippedWeapon child")
            -- (i) a WeldConstraint Part0=RightHand, Part1=weldPart was created.
            local welds = createdWelds()
            assert(#welds == 1, "exactly one WeldConstraint created, got " .. #welds)
            assert(welds[1].Part0 == rightHand, "weld Part0 == RightHand")
            assert(welds[1].Part1 == muzzle, "weld Part1 == the weldable anchor")
            -- A Model relocates as a unit via PivotTo(hand.CFrame).
            assert(clone._pivotedTo == "handCF", "model pivoted to the hand CFrame")
            -- (iii) clone BaseParts CanCollide=false, Massless=true, and the
            -- live weapon is VISIBLE + un-anchored.
            assert(muzzle.CanCollide == false, "muzzle CanCollide disabled")
            assert(muzzle.Massless == true, "muzzle Massless enabled")
            assert(muzzle.Transparency == 0, "muzzle un-hidden")
            assert(muzzle.Anchored == false, "muzzle un-anchored")
            print("OK")
        """))

    def test_multipart_hidden_anchored_model_is_unhidden_unanchored_and_rigid(self):
        # A real prefab template is HIDDEN (Transparency=1) and ANCHORED.
        # A ge-2-part Model must come back fully visible, un-anchored, pivoted to
        # the hand, with every descendant welded to the anchor and the anchor
        # welded to the hand -- not left Transparency=1 / Anchored=true with only
        # the hand weld created.
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            rightHand.CFrame = "handCF"
            addChild(character, rightHand)

            -- Two-part rifle: a Body anchor (PrimaryPart) + a Barrel descendant.
            local body, barrel
            local function cloneFactory(_)
                local m = mockInst("Rifle", "Model")
                body = mockInst("Body", "Part")
                barrel = mockInst("Barrel", "Part")
                m.PrimaryPart = body
                m._descendants = {body, barrel}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"}})

            -- Sanity: the template starts hidden + anchored.
            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone ~= nil, "equip returns the clone")

            -- Every part visible + un-anchored + collision/mass off.
            for _, part in ipairs({body, barrel}) do
                assert(part.Transparency == 0, part.Name .. " un-hidden")
                assert(part.Anchored == false, part.Name .. " un-anchored")
                assert(part.CanCollide == false, part.Name .. " CanCollide off")
                assert(part.Massless == true, part.Name .. " Massless on")
            end

            -- The whole model relocates as a unit.
            assert(clone._pivotedTo == "handCF", "model pivoted to the hand")

            -- Welds: one barrel->body (rigid), one body->hand (mount). Order is
            -- descendant-welds-first then the hand weld.
            local welds = createdWelds()
            assert(#welds == 2, "two WeldConstraints, got " .. #welds)
            -- Find the inner (anchor<->descendant) and the hand weld.
            local innerWeld, handWeld
            for _, w in ipairs(welds) do
                if w.Part0 == rightHand then handWeld = w
                elseif w.Part0 == body and w.Part1 == barrel then innerWeld = w end
            end
            assert(innerWeld ~= nil, "barrel welded to the body anchor")
            assert(handWeld ~= nil and handWeld.Part1 == body,
                "body anchor welded to the hand")
            print("OK")
        """))

    def test_no_primarypart_model_anchor_lands_on_hand(self):
        # A multi-part Model clone with NO PrimaryPart set. The equip path picks
        # the first BasePart as the anchor; it MUST pin that as PrimaryPart before
        # PivotTo so the ANCHOR lands exactly at hand.CFrame. Without the pin,
        # PivotTo uses the bounding-box pivot, leaving the anchor at an arbitrary
        # offset that the hand weld freezes (gun floats off). The parts start at
        # distinct non-origin CFrames so a bbox pivot != anchor.
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            rightHand.CFrame = "handCF"
            addChild(character, rightHand)

            -- Two-part rifle with NO PrimaryPart and parts at non-origin CFrames.
            local body, barrel
            local function cloneFactory(_)
                local m = mockInst("Rifle", "Model")
                body = mockInst("Body", "Part")
                body.CFrame = "bodyCF_nonorigin"
                barrel = mockInst("Barrel", "Part")
                barrel.CFrame = "barrelCF_nonorigin"
                m.PrimaryPart = nil  -- no PrimaryPart: bbox pivot unless pinned
                m._descendants = {body, barrel}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"}})

            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone ~= nil, "equip returns the clone")

            -- The chosen anchor is the first BasePart (body). It must have been
            -- pinned as PrimaryPart and thus pivoted exactly onto the hand.
            local welds = createdWelds()
            local handWeld
            for _, w in ipairs(welds) do
                if w.Part0 == rightHand then handWeld = w end
            end
            assert(handWeld ~= nil, "a hand weld was created")
            local anchor = handWeld.Part1
            assert(anchor == body, "anchor is the first BasePart (body)")
            -- THE LOAD-BEARING ASSERT: the anchor landed ON the hand (zero
            -- offset), not at the bounding-box pivot.
            assert(anchor.CFrame == "handCF",
                "anchor must land on hand.CFrame, got " .. tostring(anchor.CFrame))
            assert(clone.PrimaryPart == body,
                "the chosen anchor was pinned as PrimaryPart before PivotTo")
            print("OK")
        """))

    def test_bare_basepart_clone_welds_directly(self):
        # A clone that is itself a BasePart (no Model) welds directly.
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            addChild(character, rightHand)
            local function cloneFactory(_)
                local p = mockInst("BareGun", "Part")
                p._descendants = {}
                return p
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"}})
            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone ~= nil and clone.Name == "_EquippedWeapon")
            local welds = createdWelds()
            assert(welds[#welds].Part1 == clone, "bare BasePart welds itself")
            assert(clone.CanCollide == false and clone.Massless == true)
            print("OK")
        """))


# ---------------------------------------------------------------------------
# Criterion 8 — double-equip removes the prior weapon
# ---------------------------------------------------------------------------

class TestDoubleEquip:

    def test_two_equips_leave_exactly_one_weapon(self):
        _assert_ok(textwrap.dedent("""\

            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            addChild(character, rightHand)

            local seq = 0
            local function cloneFactory(_)
                seq = seq + 1
                local p = mockInst("Gun" .. seq, "Part")
                p._descendants = {}
                return p
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"}})

            local first = engine:equipWeaponOnCharacter(character, "p")
            local second = engine:equipWeaponOnCharacter(character, "p")
            assert(first ~= second, "second equip is a fresh clone")
            -- The first clone was destroyed before the second weld.
            assert(first._destroyed == true, "prior _EquippedWeapon destroyed")
            -- Exactly one _EquippedWeapon survives under the character.
            assert(character:FindFirstChild("_EquippedWeapon") == second,
                "the surviving weapon is the second clone")
            print("OK")
        """))


# ---------------------------------------------------------------------------
# Criterion 9 — R6 fallback + hard no-op
# ---------------------------------------------------------------------------

class TestRightHandFallback:

    def test_right_hand_primary(self):
        _assert_ok(textwrap.dedent("""\
            local engine = SceneRuntime.new({warn = function() end}, {})
            local char = mockInst("C", "Model")
            local rh = mockInst("RightHand", "Part")
            addChild(char, rh)
            assert(engine:_resolveRightHand(char) == rh, "R15 RightHand resolves")
            print("OK")
        """))

    def test_right_arm_fallback_when_no_right_hand(self):
        _assert_ok(textwrap.dedent("""\
            local engine = SceneRuntime.new({warn = function() end}, {})
            local char = mockInst("C", "Model")
            local arm = mockInst("Right Arm", "Part")
            addChild(char, arm)
            assert(engine:_resolveRightHand(char) == arm,
                "R6 falls back to Right Arm")
            print("OK")
        """))

    def test_no_hand_returns_nil_and_equip_no_ops(self):
        _assert_ok(textwrap.dedent("""\
            local engine = SceneRuntime.new(
                equipServices(function() return mockInst("g", "Part") end),
                {equip_prefabs = {riflePrefab = "p"}})
            local char = mockInst("C", "Model")  -- no RightHand, no Right Arm
            assert(engine:_resolveRightHand(char) == nil, "no hand -> nil")
            local before = #createdWelds()
            local result = engine:equipWeaponOnCharacter(char, "p")
            assert(result == nil, "no hand -> equip returns nil (hard no-op)")
            assert(#createdWelds() == before, "no weld created on a hard no-op")
            assert(char:FindFirstChild("_EquippedWeapon") == nil,
                "no weapon parented on a hard no-op")
            print("OK")
        """))


# ---------------------------------------------------------------------------
# Criterion 15 — survives respawn (late-arriving R15 limb)
# ---------------------------------------------------------------------------

class TestReequipOnRespawn:

    def test_reequip_r6_resolves_immediately_no_timeout_stall(self):
        # An R6 avatar has "Right Arm" and NEVER grows a "RightHand". A
        # WaitForChild("RightHand", 5)-first resolver would stall the FULL 5s on
        # every R6 respawn (unarmed for 5s) before falling back to "Right Arm".
        # The bounded POLL must check BOTH names each tick and resolve on the
        # FIRST iteration when "Right Arm" is already present (no yield consumed);
        # the assertion detects a stall via the ``_tick`` counter.
        _assert_ok(textwrap.dedent("""\
            local player = {}
            local character = mockInst("Character", "Model")
            character.Parent = mockInst("workspace", "Folder")  -- live (respawn) char
            -- R6: "Right Arm" present now, no "RightHand", and none will arrive.
            local rightArm = mockInst("Right Arm", "Part")
            rightArm.CFrame = "armCF"
            addChild(character, rightArm)

            local muzzle
            local function cloneFactory(_)
                local m = mockInst("RiflePrefabClone", "Model")
                muzzle = mockInst("Muzzle", "Part")
                m.PrimaryPart = muzzle
                m._descendants = {muzzle}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "prefab_rifle"}})

            engine:rememberEquip(player, "prefab_rifle")
            assert(_tick == 0, "precondition: no yields yet")
            engine:reequipLastWeapon(player, character)

            -- LOAD-BEARING: resolved on the first check, consuming ZERO poll
            -- yields (no 5s unarmed stall).
            assert(_tick == 0,
                "R6 must resolve immediately without consuming any poll tick, "
                .. "consumed " .. _tick)

            local welded = character:FindFirstChild("_EquippedWeapon")
            assert(welded ~= nil, "R6 weapon re-equipped immediately")
            local welds = createdWelds()
            local handWeld
            for _, w in ipairs(welds) do
                if w.Part0 == rightArm then handWeld = w end
            end
            assert(handWeld ~= nil, "weld Part0 == Right Arm (R6 grip)")
            assert(handWeld.Part1 == muzzle, "weld Part1 == the weapon anchor")
            print("OK")
        """))

    def test_reequip_waits_for_late_arriving_right_hand(self):
        # On respawn CharacterAdded fires BEFORE the R15 RightHand is parented.
        # reequipLastWeapon must POLL (bounded) so the just-spawned Character
        # (RightHand ABSENT at call time, arriving a few ticks later) still gets
        # the weapon re-equipped -- a one-shot FindFirstChild would resolve nil
        # (no weld, no _EquippedWeapon).
        _assert_ok(textwrap.dedent("""\
            local player = {}  -- opaque per-player key
            local character = mockInst("Character", "Model")
            character.Parent = mockInst("workspace", "Folder")  -- live (respawn) char
            -- RightHand is NOT a child yet; it "arrives" once the poll has
            -- yielded 3 times (modelling the limb landing a few frames later).
            local rightHand = mockInst("RightHand", "Part")
            rightHand.CFrame = "handCF"
            character:_arriveChildAtTick(rightHand, 3)
            assert(character:FindFirstChild("RightHand") == nil,
                "precondition: RightHand absent at reequip-call time")

            local muzzle
            local function cloneFactory(_)
                local m = mockInst("RiflePrefabClone", "Model")
                muzzle = mockInst("Muzzle", "Part")
                m.PrimaryPart = muzzle
                m._descendants = {muzzle}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "prefab_rifle"}})

            -- Remember the last equip, then respawn-re-equip.
            engine:rememberEquip(player, "prefab_rifle")
            engine:reequipLastWeapon(player, character)

            -- The weapon was equipped once the late hand arrived.
            assert(_tick >= 3, "poll yielded until the hand arrived, got " .. _tick)
            local welded = character:FindFirstChild("_EquippedWeapon")
            assert(welded ~= nil, "weapon re-equipped after late RightHand arrival")
            local welds = createdWelds()
            local handWeld
            for _, w in ipairs(welds) do
                if w.Part0 == rightHand then handWeld = w end
            end
            assert(handWeld ~= nil, "weld Part0 == the late-arrived RightHand")
            assert(handWeld.Part1 == muzzle, "weld Part1 == the weapon anchor")
            print("OK")
        """))

    def test_reequip_no_op_when_hand_never_arrives(self):
        # Bounded wait MISS: a Character whose RightHand never arrives (and no
        # Right Arm) must end in a clean no-op (nil resolve), not an unbounded
        # stall or a spurious weld.
        _assert_ok(textwrap.dedent("""\
            local player = {}
            local character = mockInst("Character", "Model")  -- no hand ever
            character.Parent = mockInst("workspace", "Folder")  -- live (respawn) char
            local engine = SceneRuntime.new(
                equipServices(function() return mockInst("g", "Part") end),
                {equip_prefabs = {riflePrefab = "p"}})
            engine:rememberEquip(player, "p")
            local before = #createdWelds()
            engine:reequipLastWeapon(player, character)
            assert(#createdWelds() == before, "no weld when the hand never arrives")
            assert(character:FindFirstChild("_EquippedWeapon") == nil,
                "no weapon parented when the hand never arrives")
            print("OK")
        """))

    def test_reequip_no_op_on_despawned_character(self):
        # Liveness guard: a heal-window task that fires AFTER the character left the
        # DataModel (Parent == nil) must no-op — no weld, no orphan weapon — even
        # though the hand is present, so a stale watcher fire is harmless.
        _assert_ok(textwrap.dedent("""\
            local player = {}
            local character = mockInst("Character", "Model")
            -- Despawned: NOT parented to the DataModel.
            character.Parent = nil
            local rightHand = mockInst("RightHand", "Part")
            addChild(character, rightHand)  -- hand present, but char is orphaned
            local engine = SceneRuntime.new(
                equipServices(function() return mockInst("g", "Part") end),
                {equip_prefabs = {riflePrefab = "p"}})
            engine:rememberEquip(player, "p")
            local before = #createdWelds()
            engine:reequipLastWeapon(player, character)
            assert(#createdWelds() == before, "no weld on a despawned character")
            assert(character:FindFirstChild("_EquippedWeapon") == nil,
                "no orphan weapon parented on a despawned character")
            print("OK")
        """))


# ---------------------------------------------------------------------------
# D17/Bug-2 — resolveEquipScale + weld-time ScaleTo apply
# ---------------------------------------------------------------------------

class TestResolveEquipScale:

    def test_resolve_scale_map_lookup_and_no_op_defaults(self):
        _assert_ok(textwrap.dedent("""\
            local plan = {equip_scales = {prefab_rifle = 0.2}}
            local engine = SceneRuntime.new({warn = function() end}, plan)
            assert(engine:resolveEquipScale("prefab_rifle") == 0.2,
                "known prefab_id -> its captured scale")
            assert(engine:resolveEquipScale("unknown") == 1,
                "unknown prefab_id -> 1 (no-op)")
            -- No equip_scales map at all -> 1 (the common no-scale game).
            local engine2 = SceneRuntime.new({warn = function() end}, {})
            assert(engine2:resolveEquipScale("prefab_rifle") == 1,
                "missing equip_scales map -> 1")
            -- A non-positive / non-number entry is defensively dropped to 1.
            local engine3 = SceneRuntime.new({warn = function() end},
                {equip_scales = {p = -0.5, q = "x"}})
            assert(engine3:resolveEquipScale("p") == 1, "non-positive scale -> 1")
            assert(engine3:resolveEquipScale("q") == 1, "non-number scale -> 1")
            print("OK")
        """))


class TestEquipScaleApply:

    def test_model_clone_scaled_before_weld(self):
        # A Model clone for a prefab with a captured scale must be ScaleTo'd
        # (uniform) AND still welded to the hand (scale does not break the mount).
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            rightHand.CFrame = "handCF"
            addChild(character, rightHand)

            local body
            local function cloneFactory(_)
                local m = mockInst("Rifle", "Model")
                body = mockInst("Body", "Part")
                m.PrimaryPart = body
                m._descendants = {body}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"},
                 equip_scales = {p = 0.2}})
            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone ~= nil, "equip returns the clone")
            assert(clone._scaledTo == 0.2, "Model clone ScaleTo(0.2) applied")
            assert(clone._pivotedTo == "handCF", "scaled clone still mounts on hand")
            -- Exactly one hand weld (Part0 = the hand).
            local welds = createdWelds()
            local handWeld
            for _, w in ipairs(welds) do
                if w.Part0 == rightHand then handWeld = w end
            end
            assert(handWeld ~= nil and handWeld.Part1 == body,
                "scaled model anchor welded to the hand")
            print("OK")
        """))

    def test_no_scale_entry_leaves_model_unscaled(self):
        # A prefab with NO equip_scales entry must NOT be ScaleTo'd (native size).
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            addChild(character, rightHand)
            local body
            local function cloneFactory(_)
                local m = mockInst("Rifle", "Model")
                body = mockInst("Body", "Part")
                m.PrimaryPart = body
                m._descendants = {body}
                return m
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"}})  -- no equip_scales
            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone._scaledTo == nil, "no scale entry -> ScaleTo not called")
            print("OK")
        """))

    def test_single_basepart_clone_scales_size(self):
        # A bare BasePart clone scales its Size (Size * scale), no ScaleTo.
        _assert_ok(textwrap.dedent("""\
            local character = mockInst("Character", "Model")
            local rightHand = mockInst("RightHand", "Part")
            addChild(character, rightHand)
            local function cloneFactory(_)
                local p = mockInst("Rifle", "Part")  -- bare BasePart, Size = 4
                p._descendants = {}
                return p
            end
            local engine = SceneRuntime.new(equipServices(cloneFactory),
                {equip_prefabs = {riflePrefab = "p"},
                 equip_scales = {p = 0.5}})
            local clone = engine:equipWeaponOnCharacter(character, "p")
            assert(clone ~= nil, "equip returns the bare-part clone")
            assert(clone.Size == 2, "BasePart Size scaled 4 * 0.5 = 2, got "
                .. tostring(clone.Size))
            print("OK")
        """))
