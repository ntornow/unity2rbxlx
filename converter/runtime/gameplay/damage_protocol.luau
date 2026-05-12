-- damage_protocol.luau: client + server damage routing in one file.
--
-- Owns the ``ReplicatedStorage.DamageEvent`` RemoteEvent end-to-end —
-- both the client-side fire helper and the server-side validator. The
-- two halves live in one module so the protocol shape is structurally
-- in sync; the legacy ``player_damage_remote_event`` coherence pack
-- maintained the protocol as two text blobs that drifted across codex
-- review rounds. PR #73c replaces the auto-generated
-- ``_AutoDamageEventRouter`` Script with this module.
--
-- Why route at all (with gameplay adapters on, bullet detection runs
-- server-side and applies damage directly via ``Effects.damage``):
-- Player.cs's click-to-shoot pattern is a LocalScript that raycasts
-- on click and ``:SetAttribute("TakeDamage", ...)`` on the hit
-- instance. LocalScript attribute writes don't replicate, so server-
-- side listeners (Turret.luau, NPCs) never see the hit. The legacy
-- pack's body-patch injects a ``FireServer`` call after the
-- SetAttribute; this module is the matching server router for that
-- call. The body-patch half of the legacy pack still runs when
-- adapters are on (pack scoping is in
-- ``converter.script_coherence_packs._inject_player_damage_remote_event``).
--
-- Validation pipeline (server-side):
--   1. Type guards — reject non-Instance / non-BasePart hit, non-
--      Vector3 origin/dir, zero-length dir. A malicious client can
--      ``FireServer(true)`` / ``FireServer({})`` and the server must
--      not call any Instance method on the payload.
--   2. Origin drift gate — the client-supplied camera origin must be
--      within ``MAX_ORIGIN_DRIFT_STUDS`` of the player's
--      HumanoidRootPart. Anti-teleport check; first/third-person rigs
--      can offset the camera but not by arbitrary distances.
--   3. Raycast replay — re-cast on the server from the client-
--      supplied origin + direction, with the firing player's character
--      excluded. The hit must match the intended instance (same Part
--      or same Model). Re-raycasting on the server from the
--      character's HumanoidRootPart rejects legitimate over-cover
--      shots; replaying from the camera mirrors what the client saw.
--   4. Distance gate — the replay's MAX_SHOOT_RANGE_STUDS is the cast
--      length. Anything past that range is rejected by raycast miss.
--   5. Value-preserving mirror — the client's payload is mirrored
--      verbatim onto ``hitInstance:SetAttribute("TakeDamage", value)``
--      AND onto the ancestor Model's same attribute. Non-scalar
--      payloads (table, function, Instance) are coerced to ``true``:
--      Roblox attributes only accept primitive scalars, so a
--      malicious table would crash ``SetAttribute``. Coercion keeps
--      listeners doing ``if attr then`` reacting without exposing
--      ``SetAttribute`` to invalid types.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")

local DamageProtocol = {}

local _STUDS_PER_METER: number = 3.571

-- Mirrors Unity ``Player.cs`` ``shootRange = 100`` (meters) + 50%
-- slack so a borderline-range raycast from the server doesn't drop
-- the intended hit. Codex round-10 [P2] on the legacy pack: tight
-- range matched Unity literally but dropped legitimate hits when
-- the server-side replay was a few studs shorter than the client.
local MAX_SHOOT_RANGE_STUDS: number = 100 * _STUDS_PER_METER * 1.5

-- Origin sanity bound: 20 studs covers normal first/third-person
-- camera offsets (≈ 3 character heights) without admitting arbitrary
-- teleport spoofs.
local MAX_ORIGIN_DRIFT_STUDS: number = 20

-- ``DamageEvent`` lives at the canonical ReplicatedStorage name so
-- the legacy pack's body-patched ``FireServer`` calls (and any user
-- script that referenced the legacy router by name) keep resolving.
local DAMAGE_EVENT_NAME: string = "DamageEvent"

local function _ensureRemoteEvent(): RemoteEvent
    local existing = ReplicatedStorage:FindFirstChild(DAMAGE_EVENT_NAME)
    if existing and existing:IsA("RemoteEvent") then
        return existing
    end
    if existing then
        -- A non-RemoteEvent at the canonical name would crash
        -- ``FireServer`` callers. Rename it out of the way and emit a
        -- fresh RemoteEvent — same posture the legacy router took
        -- before the adapter migration.
        existing.Name = DAMAGE_EVENT_NAME .. "_displaced"
    end
    local de = Instance.new("RemoteEvent")
    de.Name = DAMAGE_EVENT_NAME
    de.Parent = ReplicatedStorage
    return de
end

local function _matchesIntendedHit(serverHit: Instance, intendedInst: Instance): boolean
    if serverHit == intendedInst then return true end
    local serverModel = serverHit:FindFirstAncestorOfClass("Model")
    local intendedModel = intendedInst:FindFirstAncestorOfClass("Model")
    if serverModel and intendedModel and serverModel == intendedModel then
        return true
    end
    return false
end

-- Coerce arbitrary client payloads to a SetAttribute-safe value.
-- Roblox attributes accept bool / number / string (and a few typed
-- variants). Anything else becomes ``true`` so ``if attr then``
-- listeners still react.
local function _coerceTakeDamageValue(value: any): any
    local t = typeof(value)
    if t == "boolean" or t == "number" or t == "string" then
        return value
    end
    return true
end

local function _onServerEvent(
    player: Player,
    hitInstance: any,
    takeDamageValue: any,
    originPos: any,
    lookDir: any
): ()
    -- Step 1: type guards. Reject anything that would crash on the
    -- Instance / Vector3 API surface.
    if typeof(hitInstance) ~= "Instance" then return end
    if not hitInstance:IsA("BasePart") then return end
    if not hitInstance:IsDescendantOf(workspace) then return end
    if typeof(originPos) ~= "Vector3" then return end
    if typeof(lookDir) ~= "Vector3" then return end
    if lookDir.Magnitude < 1e-3 then return end

    -- Step 2: origin drift gate.
    local char = player.Character
    local hrp = char and char:FindFirstChild("HumanoidRootPart")
    if not hrp or not hrp:IsA("BasePart") then return end
    if (originPos - hrp.Position).Magnitude > MAX_ORIGIN_DRIFT_STUDS then
        return
    end

    -- Step 3 + 4: server-side raycast replay with distance gate baked
    -- into the cast length. Exclude the firing player's character so
    -- the cast doesn't self-hit.
    local rp = RaycastParams.new()
    rp.FilterDescendantsInstances = {char}
    rp.FilterType = Enum.RaycastFilterType.Exclude
    local result = workspace:Raycast(
        originPos,
        lookDir.Unit * MAX_SHOOT_RANGE_STUDS,
        rp
    )
    if not (result and _matchesIntendedHit(result.Instance, hitInstance)) then
        return
    end

    -- Step 5: value-preserving mirror.
    local mirrored = _coerceTakeDamageValue(takeDamageValue)
    hitInstance:SetAttribute("TakeDamage", mirrored)
    local m = hitInstance:FindFirstAncestorOfClass("Model")
    if m then
        m:SetAttribute("TakeDamage", mirrored)
    end
end

-- Client-side fire helper. AI bodies / future api_mappings entries
-- call this instead of inlining ``FireServer`` so the payload shape
-- stays in sync with the server-side guards. ``takeDamageValue`` is
-- whatever the client wrote to the hit instance's attribute — passing
-- it through preserves the existing "increment counter" / "literal
-- true" / numeric semantics the legacy pack supported.
function DamageProtocol.fire(hitInstance: Instance, takeDamageValue: any): ()
    if not RunService:IsClient() then
        warn(
            "[gameplay/damage_protocol] fire() called on server — "
            .. "this is a client-only helper."
        )
        return
    end
    local de = ReplicatedStorage:FindFirstChild(DAMAGE_EVENT_NAME)
    if not (de and de:IsA("RemoteEvent")) then
        return
    end
    local cam = workspace.CurrentCamera
    if cam == nil then return end
    de:FireServer(
        hitInstance,
        takeDamageValue,
        cam.CFrame.Position,
        cam.CFrame.LookVector
    )
end

-- Server-side init. Idempotent: if the OnServerEvent connection is
-- already attached (re-require during a soft reload), skip.
local _serverInitialized: boolean = false

local function _initServer(): ()
    if _serverInitialized then return end
    if not RunService:IsServer() then return end
    local de = _ensureRemoteEvent()
    de.OnServerEvent:Connect(_onServerEvent)
    _serverInitialized = true
end

_initServer()

-- Exposed for tests + future api_mappings consumers.
DamageProtocol._coerceTakeDamageValue = _coerceTakeDamageValue
DamageProtocol._matchesIntendedHit = _matchesIntendedHit
DamageProtocol.MAX_SHOOT_RANGE_STUDS = MAX_SHOOT_RANGE_STUDS
DamageProtocol.MAX_ORIGIN_DRIFT_STUDS = MAX_ORIGIN_DRIFT_STUDS
DamageProtocol.DAMAGE_EVENT_NAME = DAMAGE_EVENT_NAME

-- Test-only re-init hook. Real Roblox runtimes never call this.
function DamageProtocol._resetForTest(): ()
    _serverInitialized = false
end

return DamageProtocol
