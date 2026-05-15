-- Services
local Players = game:GetService("Players")

-- Configuration (from serialized fields)
local closeTime = 5

-- Container resolution: this script may be parented to a Model that
-- wraps the trigger zone, or directly to a BasePart trigger.
local container = script.Parent

local TRIGGER_NAMES = {"Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume", "PickupTouchDetector"}
local function findTriggerPart(parent)
    for _, n in ipairs(TRIGGER_NAMES) do
        local p = parent:FindFirstChild(n)
        if p and p:IsA("BasePart") then return p end
    end
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency >= 1 and not c:IsA("MeshPart") then
            return c
        end
    end
    return parent:FindFirstChildWhichIsA("BasePart")
end

local triggerPart
if container:IsA("BasePart") then
    triggerPart = container
else
    triggerPart = findTriggerPart(container)
end

-- doorAnim equivalent: Unity's transform.parent.Find("door") returns the
-- sibling GameObject named "door". The runtime animator script on that
-- door listens for an "open" attribute change (Animator.SetBool analogue).
local function getDoorTarget()
    local doorParent = container.Parent
    if not doorParent then return nil end
    return doorParent:FindFirstChild("door")
end

-- AudioSource equivalent: a Sound child on the trigger part (or container).
local function getSound()
    if triggerPart then
        local s = triggerPart:FindFirstChildWhichIsA("Sound")
        if s then return s end
    end
    return container:FindFirstChildWhichIsA("Sound")
end

-- Track which players are currently inside the trigger so we only fire
-- the toggle once on enter / once on exit (Touched fires repeatedly).
local insidePlayers = {}

-- Drive the door tween directly. The earlier indirect ``open`` attribute
-- path was meant to defer to ``Anim_*_door_open/close`` drivers emitted by
-- the animation phase, but those drivers ship a (0,0,0) close offset (no-op)
-- and an unscaled +4 stud open offset (the AnimClip's raw Unity meters,
-- never multiplied by STUDS_PER_METER), so they can't actually open the
-- door visibly. Tweening here keeps the open/close motion correct and
-- self-contained, and the ``open`` attribute is still flipped so any
-- consumers that watch it (e.g., audio cue scripts) keep working.
local TweenService = game:GetService("TweenService")
local _STUDS_PER_METER = 3.571
local _OPEN_OFFSET = Vector3.new(0, 4 * _STUDS_PER_METER, 0)
local _closedCFrame
local _openCFrame
local _currentTween
local function _ensureBaseline()
    if _closedCFrame then return end
    local doorTarget = getDoorTarget()
    if doorTarget and doorTarget:IsA("BasePart") then
        _closedCFrame = doorTarget.CFrame
        _openCFrame = _closedCFrame + _OPEN_OFFSET
    end
end

local function toggleDoor(value)
    _ensureBaseline()
    local doorTarget = getDoorTarget()
    if doorTarget then
        doorTarget:SetAttribute("open", value)
        if _closedCFrame and doorTarget:IsA("BasePart") then
            if _currentTween then _currentTween:Cancel() end
            _currentTween = TweenService:Create(
                doorTarget,
                TweenInfo.new(1, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
                { CFrame = value and _openCFrame or _closedCFrame }
            )
            _currentTween:Play()
        end
    end
    local sound = getSound()
    if sound then
        sound.PlaybackSpeed = math.random() * 0.4 + 0.8
        sound:Play()
    end
end

local function getPlayerFromPart(part)
    local character = part:FindFirstAncestorOfClass("Model")
    if not character then return nil end
    return Players:GetPlayerFromCharacter(character)
end

if triggerPart then
    triggerPart.Touched:Connect(function(other)
        local player = getPlayerFromPart(other)
        if not player then return end
        if insidePlayers[player] then return end
        insidePlayers[player] = true
        if player:GetAttribute("hasKey") == true then
            toggleDoor(true)
        end
    end)
    triggerPart.TouchEnded:Connect(function(other)
        local player = getPlayerFromPart(other)
        if not player then return end
        if not insidePlayers[player] then return end
        insidePlayers[player] = nil
        if player:GetAttribute("hasKey") == true then
            toggleDoor(false)
        end
    end)
end

-- Unity's Machine.cs opens doors via ``doors[i].SendMessage("ToggleDoor", true)``.
-- Machine.luau transpiled that to ``door:SetAttribute("ToggleDoor", value)`` on
-- the Door Model instance. SendMessage doesn't exist in Roblox; attribute-based
-- signalling is the idiomatic replacement, but it only works if the receiver
-- listens. Wire the listener here so the Machine→Door progression (place items
-- on a Machine → open the gated door) actually works. Distinct from the
-- hasKey-on-touch path above: that path is dormant in scenes without a Key
-- pickup.
local doorModel = container.Parent
if doorModel then
    doorModel:GetAttributeChangedSignal("ToggleDoor"):Connect(function()
        toggleDoor(doorModel:GetAttribute("ToggleDoor") == true)
    end)
end

-- _AutoFpsDoorTweenInjected: door coherence pack
-- Listens to ``open`` attribute change on the sibling ``door`` mesh and
-- tweens it +14.28 studs Y (Unity 4m × STUDS_PER_METER) on open, back
-- on close. Mecanim Animator → TweenService bridge.
--
-- Always wires the tween. The earlier ``_hasAnimDriver`` deferral was a
-- false safety: the animation phase's auto-generated ``Anim_*_door_open``
-- driver tweens by an unscaled +4 studs (raw Unity meters, missing
-- STUDS_PER_METER), and its companion ``Anim_*_door_close`` ships a
-- (0,0,0) close offset, so deferring left doors with imperceptible
-- motion (or none at all). Tweening here unconditionally fixes the open
-- motion; the AnimClip drivers' +4 stud overshoot is small enough that
-- co-existence is a non-issue in practice.
do
    local TweenService = game:GetService("TweenService")
    local _doorContainer = script.Parent
    local _parent = _doorContainer and _doorContainer.Parent
    local _doorMesh = _parent and _parent:FindFirstChild("door")
    if _doorMesh and _doorMesh:IsA("BasePart") then
        local _STUDS_PER_METER = 3.571
        local _OPEN_OFFSET = Vector3.new(0, 4 * _STUDS_PER_METER, 0)
        local _closedCFrame = _doorMesh.CFrame
        local _openCFrame = _closedCFrame + _OPEN_OFFSET
        local _currentTween
        local function _animateTo(target)
            if _currentTween then _currentTween:Cancel() end
            _currentTween = TweenService:Create(
                _doorMesh,
                TweenInfo.new(1, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
                { CFrame = target }
            )
            _currentTween:Play()
        end
        _doorMesh:GetAttributeChangedSignal("open"):Connect(function()
            if _doorMesh:GetAttribute("open") then
                _animateTo(_openCFrame)
            else
                _animateTo(_closedCFrame)
            end
        end)
    end
end

-- _AutoMachineDoorSignal: Machine→Door coherence pack
-- Unity's Machine.cs calls ``doors[i].SendMessage("ToggleDoor", true)`` to
-- progress players past gated doors after they place items on the machine.
-- Machine.luau transpiled that to ``door:SetAttribute("ToggleDoor", value)``
-- on this Door Model. SendMessage doesn't exist in Roblox; the attribute is
-- the idiomatic replacement, but it only works if the receiver listens.
-- Wire that here so the Machine→Door progression works (distinct from the
-- hasKey-on-touch path, which is dormant in scenes without a Key pickup).
do
    local _machineDoorModel = script.Parent and script.Parent.Parent
    if _machineDoorModel then
        _machineDoorModel:GetAttributeChangedSignal("ToggleDoor"):Connect(function()
            local v = _machineDoorModel:GetAttribute("ToggleDoor")
            toggleDoor(v == true)
        end)
    end
end
