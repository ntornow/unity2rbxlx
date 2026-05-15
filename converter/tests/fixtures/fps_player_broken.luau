-- Synthetic broken FPS Player.luau preserved as a regression fixture
-- for converter/converter/semantic_validators.py. Captures the four
-- classes of bug surfaced during the SimpleFPS rifle debugging session
-- (2026-05-15): each one is independently sufficient to break the
-- view-model.
--
-- This file is NOT executed; the tests load it as a string and pass it
-- through run_semantic_validators(). Do not edit without updating the
-- assertions in tests/test_semantic_validators.py.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")

local localPlayer = Players.LocalPlayer
local camera = workspace.CurrentCamera
local character

UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter

local weaponSlot = Instance.new("Attachment")

local function onCharacterAdded(char)
    character = char
    local head = char:WaitForChild("Head")
    weaponSlot.Position = Vector3.new(0.4, -0.64, 0.6)
    weaponSlot.Parent = head
    -- Rule 6 trigger: bare meter offset on camera Y.
    camera.CFrame = CFrame.new(head.Position + Vector3.new(0, 0.6, 0))
end

localPlayer.CharacterAdded:Connect(onCharacterAdded)

local function getRifle()
    local templates = ReplicatedStorage:WaitForChild("Templates")
    local riflePrefab = templates:FindFirstChild("Rifle")
    if not riflePrefab then return end

    local rifle = riflePrefab:Clone()
    for _, p in rifle:GetDescendants() do
        if p:IsA("BasePart") then
            -- Rule 2: anchored false in a context with no welds in the script.
            p.Anchored = false
            p.CanCollide = false
        end
    end

    -- Rule 1: parent walks through Character (weaponSlot.Parent == head).
    rifle.Parent = weaponSlot.Parent
    -- Rule 3: sub-1 magnitudes used as a CFrame translation,
    -- against a script that elsewhere works in studs (>= 10).
    rifle:PivotTo(CFrame.new(0.4, -0.35, -0.8))
    -- Rule 5: positioned from camera.CFrame ONCE with no per-frame
    -- update — the muzzle flare won't track view direction.
    local flare = Instance.new("Part")
    flare:PivotTo(camera.CFrame * CFrame.new(0, 0, -3))
end

-- Studs-scale anchor so rule 3 has a context value. Without this the
-- script could be in a meter convention and the rule would suppress.
local spawnPos = Vector3.new(120, 5, 47)
local _ = spawnPos
