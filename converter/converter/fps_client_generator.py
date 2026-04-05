"""
fps_client_generator.py -- Generate FPS client controller and HUD for converted games.

When the transpiled scripts contain a Player server script that uses RemoteEvents
for shooting, health, ammo, etc., this module generates the corresponding
client-side LocalScript and ScreenGui HUD that the server script expects.

This bridges the gap between Unity's single-script-per-object model and Roblox's
client/server split architecture.
"""

from __future__ import annotations

import logging
from core.roblox_types import RbxScript, RbxScreenGui, RbxUIElement, RbxPlace

log = logging.getLogger(__name__)


def detect_fps_game(place: RbxPlace) -> bool:
    """Check if the transpiled scripts indicate an FPS-style game.

    Looks for shooting mechanics (raycast-based), health/ammo systems,
    or RemoteEvent-based FPS patterns.
    """
    for script in place.scripts:
        src = script.source
        src_lower = src.lower()
        # Server-authoritative FPS (RemoteEvent pattern)
        if "PlayerShoot" in src and "RemoteEvent" in src:
            return True
        # Client-side FPS (direct shooting via Raycast + ammo tracking)
        if "Raycast" in src and ("curAmmo" in src or "ammo" in src_lower) and "shoot" in src_lower:
            return True
        # FPS with health + weapon pickup
        if "curHealth" in src and "gotWeapon" in src and "Raycast" in src:
            return True
    return False


def _has_client_fps_controller(place: RbxPlace) -> bool:
    """Check if a client-side FPS controller LocalScript already exists.

    Only LocalScripts auto-run on the client. ModuleScripts with FPS logic
    won't handle input unless explicitly required by a LocalScript, so they
    don't count as a working client controller.
    """
    for script in place.scripts:
        if getattr(script, "script_type", "") != "LocalScript":
            continue
        src = script.source
        if ("UserInputService" in src and "MouseButton1" in src and
                ("Raycast" in src or "shoot" in src.lower())):
            return True
    return False


def _has_hud_screen_gui(place: RbxPlace) -> bool:
    """Check if a HUD ScreenGui already exists from Canvas conversion."""
    for sg in place.screen_guis:
        if sg.name == "HUD":
            return True
    return False


def generate_fps_client_script() -> RbxScript:
    """Generate a LocalScript that handles FPS input: mouse look, shooting, item pickup."""
    source = '''\
-- FPS Client Controller (auto-generated)
-- Handles mouse look, shooting input, and item pickup triggers.

local Players = game:GetService("Players")
local UserInputService = game:GetService("UserInputService")
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local player = Players.LocalPlayer
local mouse = player:GetMouse()

-- Remote events (created by server Player script)
local function waitForRemote(name)
    return ReplicatedStorage:WaitForChild(name, 10)
end

local ShootRemote = waitForRemote("PlayerShoot")
local GetItemRemote = waitForRemote("PlayerGetItem")

-- Mouse lock for FPS
UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
UserInputService.MouseIconEnabled = false

-- Camera variables
local SENSITIVITY = 0.002
local pitchAngle = 0
local yawAngle = 0
local MAX_PITCH = math.rad(80)

-- First-person camera: follow character head with mouse look
local camera = workspace.CurrentCamera
camera.CameraType = Enum.CameraType.Scriptable

local function updateCamera()
	local character = player.Character
	if not character then return end
	local head = character:FindFirstChild("Head")
	if not head then return end

	local delta = UserInputService:GetMouseDelta()
	yawAngle = yawAngle - delta.X * SENSITIVITY
	pitchAngle = math.clamp(pitchAngle - delta.Y * SENSITIVITY, -MAX_PITCH, MAX_PITCH)

	local headPos = head.Position + Vector3.new(0, 0.5, 0)
	camera.CFrame = CFrame.new(headPos)
		* CFrame.Angles(0, yawAngle, 0)
		* CFrame.Angles(pitchAngle, 0, 0)
end

RunService.RenderStepped:Connect(updateCamera)

-- Shooting
local SHOOT_COOLDOWN = 0.1
local lastShot = 0

UserInputService.InputBegan:Connect(function(input, gameProcessed)
    if gameProcessed then return end

    if input.UserInputType == Enum.UserInputType.MouseButton1 then
        local now = tick()
        if now - lastShot < SHOOT_COOLDOWN then return end
        lastShot = now

        if not camera then return end

        local origin = camera.CFrame.Position
        local direction = camera.CFrame.LookVector

        if ShootRemote then
            ShootRemote:FireServer(origin, direction)
        end
    end
end)

-- ESC to toggle pause
local isPaused = false
UserInputService.InputBegan:Connect(function(input, gameProcessed)
    if input.KeyCode == Enum.KeyCode.Escape then
        isPaused = not isPaused
        if isPaused then
            UserInputService.MouseBehavior = Enum.MouseBehavior.Default
            UserInputService.MouseIconEnabled = true
            camera.CameraType = Enum.CameraType.Custom
        else
            UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
            UserInputService.MouseIconEnabled = false
            camera.CameraType = Enum.CameraType.Scriptable
        end
    end
end)

-- WASD movement direction relative to camera facing
local function getMovementDirection()
    local moveDir = Vector3.zero
    if UserInputService:IsKeyDown(Enum.KeyCode.W) then
        moveDir = moveDir + Vector3.new(0, 0, -1)
    end
    if UserInputService:IsKeyDown(Enum.KeyCode.S) then
        moveDir = moveDir + Vector3.new(0, 0, 1)
    end
    if UserInputService:IsKeyDown(Enum.KeyCode.A) then
        moveDir = moveDir + Vector3.new(-1, 0, 0)
    end
    if UserInputService:IsKeyDown(Enum.KeyCode.D) then
        moveDir = moveDir + Vector3.new(1, 0, 0)
    end
    if moveDir.Magnitude > 0 then
        -- Rotate movement direction by camera yaw
        moveDir = (CFrame.Angles(0, yawAngle, 0) * moveDir).Unit
    end
    return moveDir
end

-- Apply movement direction to humanoid + jump support
RunService.Heartbeat:Connect(function()
    local character = player.Character
    if not character or isPaused then return end
    local humanoid = character:FindFirstChildOfClass("Humanoid")
    if not humanoid then return end
    humanoid:Move(getMovementDirection())
end)

-- Jump on space
UserInputService.InputBegan:Connect(function(input, gameProcessed)
    if gameProcessed or isPaused then return end
    if input.KeyCode == Enum.KeyCode.Space then
        local character = player.Character
        if character then
            local humanoid = character:FindFirstChildOfClass("Humanoid")
            if humanoid then
                humanoid.Jump = true
            end
        end
    end
end)

-- Item pickup handling
-- Detect pickups by both name pattern and IsPickup attribute for flexibility.
-- The server Pickup script handles the actual touch-based collection;
-- this client-side code highlights nearby pickups and handles UI feedback.

local function isPickupPart(part)
    -- Check attribute first (set by converter for prefab instances)
    if part:GetAttribute("IsPickup") then
        return true
    end
    -- Fallback: check if name contains "Pickup" (case-insensitive)
    if string.find(string.lower(part.Name), "pickup") then
        return true
    end
    -- Check parent Model name
    if part.Parent and part.Parent:IsA("Model") then
        if part.Parent:GetAttribute("IsPickup") then
            return true
        end
        if string.find(string.lower(part.Parent.Name), "pickup") then
            return true
        end
    end
    return false
end

local function isSpawnPoint(part)
    if part:GetAttribute("IsSpawnPoint") then
        return true
    end
    if string.find(string.lower(part.Name), "spawnpoint") or string.find(string.lower(part.Name), "spawn_point") then
        return true
    end
    if part.Name == "SpawnPoint" then
        return true
    end
    return false
end

-- Highlight nearby pickups with a SelectionBox (visual feedback)
local character = player.Character or player.CharacterAdded:Wait()
local function setupPickupHighlights()
    for _, obj in ipairs(workspace:GetDescendants()) do
        if obj:IsA("BasePart") and isPickupPart(obj) then
            local highlight = Instance.new("Highlight")
            highlight.Name = "PickupHighlight"
            highlight.FillColor = Color3.new(1, 1, 0.3)
            highlight.FillTransparency = 0.7
            highlight.OutlineTransparency = 0.5
            highlight.Adornee = obj.Parent:IsA("Model") and obj.Parent or obj
            highlight.Parent = obj.Parent:IsA("Model") and obj.Parent or obj
        end
    end
end

task.defer(setupPickupHighlights)
'''
    return RbxScript(
        name="FPSController",
        source=source,
        script_type="LocalScript",
    )


def generate_hud_screen_gui() -> RbxScreenGui:
    """Generate a ScreenGui with health bar, ammo counter, crosshair, and item indicators."""

    # Crosshair (center dot)
    crosshair = RbxUIElement(
        class_name="TextLabel",
        name="Crosshair",
        position=(0.5, -4, 0.5, -4),
        size=(0, 8, 0, 8),
        background_color=(1, 1, 1),
        background_transparency=0.0,
        text="+",
        text_color=(1, 1, 1),
        text_size=18,
    )

    # Health bar background
    health_fill = RbxUIElement(
        class_name="Frame",
        name="Fill",
        position=(0, 0, 0, 0),
        size=(1, 0, 1, 0),
        background_color=(0.2, 0.8, 0.2),
        background_transparency=0.0,
    )

    health_bar = RbxUIElement(
        class_name="Frame",
        name="Health",
        position=(0, 10, 1, -50),
        size=(0, 200, 0, 20),
        background_color=(0.3, 0.3, 0.3),
        background_transparency=0.3,
        children=[health_fill],
    )

    # Ammo counter
    ammo_cur = RbxUIElement(
        class_name="TextLabel",
        name="Cur",
        position=(0, 0, 0, 0),
        size=(0.45, 0, 1, 0),
        background_transparency=1.0,
        text="0",
        text_color=(1, 1, 1),
        text_size=24,
    )

    ammo_slash = RbxUIElement(
        class_name="TextLabel",
        name="Slash",
        position=(0.45, 0, 0, 0),
        size=(0.1, 0, 1, 0),
        background_transparency=1.0,
        text="/",
        text_color=(0.7, 0.7, 0.7),
        text_size=20,
    )

    ammo_total = RbxUIElement(
        class_name="TextLabel",
        name="Total",
        position=(0.55, 0, 0, 0),
        size=(0.45, 0, 1, 0),
        background_transparency=1.0,
        text="250",
        text_color=(0.7, 0.7, 0.7),
        text_size=20,
    )

    ammo_frame = RbxUIElement(
        class_name="Frame",
        name="Ammo",
        position=(1, -160, 1, -50),
        size=(0, 150, 0, 30),
        background_color=(0.1, 0.1, 0.1),
        background_transparency=0.5,
        children=[ammo_cur, ammo_slash, ammo_total],
    )

    # Item indicators (battery, small battery, medium battery, gas can)
    item_names = ["Battery", "SmallBattery", "MediumBattery", "GasCan"]
    item_children = []
    for i, item_name in enumerate(item_names):
        indicator = RbxUIElement(
            class_name="TextLabel",
            name=item_name,
            position=(0, i * 80, 0, 0),
            size=(0, 75, 0, 30),
            background_color=(0.2, 0.2, 0.2),
            background_transparency=0.5,
            text=item_name,
            text_color=(0.6, 0.6, 0.6),
            text_size=11,
        )
        item_children.append(indicator)

    item_module = RbxUIElement(
        class_name="Frame",
        name="ItemModule",
        position=(0.5, -160, 1, -50),
        size=(0, 320, 0, 30),
        background_transparency=1.0,
        children=item_children,
    )

    # Pause menu (hidden by default)
    resume_button = RbxUIElement(
        class_name="TextButton",
        name="ResumeButton",
        position=(0.5, -75, 0.5, -20),
        size=(0, 150, 0, 40),
        background_color=(0.2, 0.6, 0.2),
        background_transparency=0.0,
        text="Resume",
        text_color=(1, 1, 1),
        text_size=18,
    )

    pause_menu = RbxUIElement(
        class_name="Frame",
        name="Pause",
        position=(0, 0, 0, 0),
        size=(1, 0, 1, 0),
        background_color=(0, 0, 0),
        background_transparency=0.5,
        visible=False,
        children=[resume_button],
    )

    # Main HUD container (Module)
    module = RbxUIElement(
        class_name="Frame",
        name="Module",
        position=(0, 0, 0, 0),
        size=(1, 0, 1, 0),
        background_transparency=1.0,
        children=[health_bar, ammo_frame],
    )

    screen_gui = RbxScreenGui(name="HUD")
    screen_gui.elements = [crosshair, module, item_module, pause_menu]

    return screen_gui


def generate_hud_client_script() -> RbxScript:
    """Generate a LocalScript that updates the HUD based on RemoteEvents from the server."""
    source = '''\
-- HUD Controller (auto-generated)
-- Updates health bar, ammo counter, and item indicators from server events.
-- Adapts to both auto-generated and Canvas-converted HUD layouts.

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local UserInputService = game:GetService("UserInputService")

local player = Players.LocalPlayer
local playerGui = player:WaitForChild("PlayerGui")
local hudGui = playerGui:WaitForChild("HUD", 10)

if not hudGui then
    warn("HUD ScreenGui not found")
    return
end

local module = hudGui:FindFirstChild("Module")
local itemModule = hudGui:FindFirstChild("ItemModule")

-- Find health fill element (adapts to different HUD layouts)
local healthFill = nil
if module then
    local healthBar = module:FindFirstChild("Health")
    if healthBar then
        -- Try Canvas-converted layout: Health > Back > CurHealth
        local back = healthBar:FindFirstChild("Back")
        if back then
            healthFill = back:FindFirstChild("CurHealth")
        end
        -- Try auto-generated layout: Health > Fill
        if not healthFill then
            healthFill = healthBar:FindFirstChild("Fill")
        end
    end
end

-- Find ammo text element
local ammoCur = nil
if module then
    local ammoFrame = module:FindFirstChild("Ammo")
    if ammoFrame then
        ammoCur = ammoFrame:FindFirstChild("Cur")
    end
end

local MAX_HEALTH = 100

-- Wait for remote events from server
local function waitForRemote(name)
    return ReplicatedStorage:WaitForChild(name, 10)
end

local HealthUpdateRemote = waitForRemote("HealthUpdate")
local AmmoUpdateRemote = waitForRemote("AmmoUpdate")
local ItemUpdateRemote = waitForRemote("ItemUpdate")

-- Health update
if HealthUpdateRemote and healthFill then
    HealthUpdateRemote.OnClientEvent:Connect(function(curHealth)
        local pct = math.clamp(curHealth / MAX_HEALTH, 0, 1)
        healthFill.Size = UDim2.new(pct, 0, 1, 0)

        -- Color shift: green -> yellow -> red
        if pct > 0.5 then
            healthFill.BackgroundColor3 = Color3.new(0.2, 0.8, 0.2)
        elseif pct > 0.25 then
            healthFill.BackgroundColor3 = Color3.new(0.9, 0.7, 0.1)
        else
            healthFill.BackgroundColor3 = Color3.new(0.9, 0.2, 0.2)
        end
    end)
end

-- Ammo update
if AmmoUpdateRemote and ammoCur then
    AmmoUpdateRemote.OnClientEvent:Connect(function(curAmmo)
        ammoCur.Text = tostring(curAmmo)
    end)
end

-- Item collected
if ItemUpdateRemote and itemModule then
    ItemUpdateRemote.OnClientEvent:Connect(function(itemName)
        local indicator = itemModule:FindFirstChild(itemName)
        if indicator then
            indicator.BackgroundColor3 = Color3.new(0.2, 0.7, 0.2)
            indicator.TextColor3 = Color3.new(1, 1, 1)
            indicator.BackgroundTransparency = 0.1
        end
    end)
end

-- Pause menu toggle
local pauseMenu = hudGui:FindFirstChild("Pause")
if pauseMenu then
    local isPaused = false
    local resumeButton = pauseMenu:FindFirstChild("ResumeButton")

    local function togglePause()
        isPaused = not isPaused
        pauseMenu.Visible = isPaused
    end

    UserInputService.InputBegan:Connect(function(input, gameProcessed)
        if input.KeyCode == Enum.KeyCode.Escape then
            togglePause()
        end
    end)

    if resumeButton then
        resumeButton.Activated:Connect(function()
            isPaused = false
            pauseMenu.Visible = false
            UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
            UserInputService.MouseIconEnabled = false
        end)
    end
end
'''
    return RbxScript(
        name="HUDController",
        source=source,
        script_type="LocalScript",
    )


def generate_game_server_script() -> RbxScript:
    """Generate a server Script that handles basic game infrastructure.

    This script runs in ServerScriptService and handles:
    - Player respawn at spawn points
    - Loading custom character properties from converted data
    - Initial game state setup
    """
    source = '''\
-- Game Server Manager (auto-generated by Unity converter)
-- Handles spawn system, player initialization, and game state.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

-- Find the best spawn point in workspace
local function findSpawnPoint()
    -- Look for SpawnLocation first
    for _, obj in ipairs(workspace:GetDescendants()) do
        if obj:IsA("SpawnLocation") then
            return obj.CFrame + Vector3.new(0, 3, 0)
        end
    end
    -- Fallback: look for parts named "SpawnPoint" or with IsSpawnPoint attribute
    for _, obj in ipairs(workspace:GetDescendants()) do
        if obj:IsA("BasePart") then
            if obj:GetAttribute("IsSpawnPoint") or string.find(string.lower(obj.Name), "spawn") then
                return obj.CFrame + Vector3.new(0, 3, 0)
            end
        end
    end
    -- Default spawn above origin
    return CFrame.new(0, 10, 0)
end

local spawnCFrame = findSpawnPoint()

-- Setup each player when they join
local function onPlayerAdded(player)
    player.CharacterAdded:Connect(function(character)
        -- Teleport to spawn point
        local hrp = character:WaitForChild("HumanoidRootPart", 5)
        if hrp then
            task.wait(0.1) -- Wait for character to load
            hrp.CFrame = spawnCFrame
        end

        -- Apply character properties from converter attributes
        local humanoid = character:FindFirstChildOfClass("Humanoid")
        if humanoid then
            -- Check for custom walk speed from NavMeshAgent or CharacterController
            for _, obj in ipairs(workspace:GetDescendants()) do
                if obj:IsA("BasePart") then
                    local walkSpeed = obj:GetAttribute("_WalkSpeed")
                    if walkSpeed and obj:GetAttribute("_HasCharacterController") then
                        humanoid.WalkSpeed = walkSpeed
                        local jumpHeight = obj:GetAttribute("_JumpHeight")
                        if jumpHeight then
                            humanoid.JumpHeight = jumpHeight
                        end
                        local slopeAngle = obj:GetAttribute("_MaxSlopeAngle")
                        if slopeAngle then
                            humanoid.MaxSlopeAngle = slopeAngle
                        end
                        local hipHeight = obj:GetAttribute("_HipHeight")
                        if hipHeight then
                            humanoid.HipHeight = hipHeight
                        end
                        -- Check for health data from converted MonoBehaviour
                        local maxHealth = obj:GetAttribute("MaxHealth") or obj:GetAttribute("maxHitPoints") or obj:GetAttribute("MaxHP")
                        if maxHealth and maxHealth > 0 then
                            humanoid.MaxHealth = maxHealth
                            humanoid.Health = maxHealth
                        end
                        break
                    end
                end
            end
        end
    end)
end

Players.PlayerAdded:Connect(onPlayerAdded)
-- Handle players already in game (Studio test mode)
for _, player in ipairs(Players:GetPlayers()) do
    task.spawn(onPlayerAdded, player)
end

-- Create RemoteEvents for FPS mechanics
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local shootRemote = Instance.new("RemoteEvent")
shootRemote.Name = "PlayerShoot"
shootRemote.Parent = ReplicatedStorage

local getItemRemote = Instance.new("RemoteEvent")
getItemRemote.Name = "PlayerGetItem"
getItemRemote.Parent = ReplicatedStorage

-- Handle shooting: client sends origin + direction, server does raycast + damage
local SHOOT_RANGE = 1000
local SHOOT_DAMAGE = 25

shootRemote.OnServerEvent:Connect(function(player, origin, direction)
    if typeof(origin) ~= "Vector3" or typeof(direction) ~= "Vector3" then return end
    -- Sanity: origin should be near the player
    local char = player.Character
    if not char then return end
    local head = char:FindFirstChild("Head")
    if head and (head.Position - origin).Magnitude > 20 then return end

    local rayParams = RaycastParams.new()
    rayParams.FilterType = Enum.RaycastFilterType.Exclude
    rayParams.FilterDescendantsInstances = {char}
    local result = workspace:Raycast(origin, direction.Unit * SHOOT_RANGE, rayParams)

    if result and result.Instance then
        -- Check if we hit a humanoid (NPC or player)
        local hitPart = result.Instance
        local hitModel = hitPart:FindFirstAncestorOfClass("Model")
        local hitHumanoid = hitModel and hitModel:FindFirstChildOfClass("Humanoid")
        if hitHumanoid then
            hitHumanoid:TakeDamage(SHOOT_DAMAGE)
        end
        -- Visual: brief highlight on hit part
        task.spawn(function()
            local orig = hitPart.Color
            hitPart.Color = Color3.new(1, 0.3, 0.3)
            task.wait(0.1)
            pcall(function() hitPart.Color = orig end)
        end)
    end
end)

-- Handle item pickup: player touches a pickup part
getItemRemote.OnServerEvent:Connect(function(player, pickupPart)
    if typeof(pickupPart) ~= "Instance" or not pickupPart:IsA("BasePart") then return end
    if not pickupPart.Parent then return end
    -- Check distance
    local char = player.Character
    if not char then return end
    local hrp = char:FindFirstChild("HumanoidRootPart")
    if not hrp or (hrp.Position - pickupPart.Position).Magnitude > 20 then return end
    -- Apply pickup effect based on name
    local name = pickupPart.Name:lower()
    local humanoid = char:FindFirstChildOfClass("Humanoid")
    if humanoid then
        if name:find("health") or name:find("hp") or name:find("medkit") then
            humanoid.Health = math.min(humanoid.Health + 25, humanoid.MaxHealth)
        end
    end
    -- Remove pickup (it will respawn if ObjectResetter is active)
    pickupPart.Parent = nil
end)

print("[GameServer] Initialized. Spawn point at", spawnCFrame.Position)
'''
    return RbxScript(
        name="GameServerManager",
        source=source,
        script_type="Script",
    )


def generate_collision_group_script() -> RbxScript:
    """Generate a server Script that creates CollisionGroups from Unity layer attributes.

    Unity's layer system maps to Roblox's CollisionGroup system.
    Parts with UnityLayer attributes get assigned to named collision groups.
    """
    source = '''\
-- CollisionGroup Setup (auto-generated from Unity layers)
-- Maps UnityLayer attributes on parts to Roblox CollisionGroups.

local PhysicsService = game:GetService("PhysicsService")

-- Standard Unity layer names (index -> name)
local LAYER_NAMES = {
    [0] = "Default",
    [1] = "TransparentFX",
    [2] = "IgnoreRaycast",
    [4] = "Water",
    [5] = "UI",
    [8] = "Terrain",
}

-- Create collision groups for layers found in the scene
local createdGroups = {}

local function ensureGroup(layerIdx)
    if createdGroups[layerIdx] then return createdGroups[layerIdx] end
    local name = LAYER_NAMES[layerIdx] or ("UnityLayer" .. tostring(layerIdx))
    pcall(function()
        PhysicsService:RegisterCollisionGroup(name)
    end)
    createdGroups[layerIdx] = name
    return name
end

-- Scan all parts and assign CollisionGroups based on UnityLayer
task.wait(1)

for _, part in workspace:GetDescendants() do
    if part:IsA("BasePart") then
        local layer = part:GetAttribute("UnityLayer")
        if layer and layer ~= 0 then
            local groupName = ensureGroup(layer)
            part.CollisionGroup = groupName
        end
    end
end

-- Common Unity collision exclusions:
-- Layer 2 (IgnoreRaycast) doesn't collide with anything
if createdGroups[2] then
    for _, group in createdGroups do
        pcall(function()
            PhysicsService:CollisionGroupSetCollidable(createdGroups[2], group, false)
        end)
    end
end

print("[CollisionGroups] Setup complete:", createdGroups)
'''
    return RbxScript(
        name="CollisionGroupSetup",
        source=source,
        script_type="Script",
    )


def inject_fps_scripts(place: RbxPlace) -> int:
    """If the game is FPS-style, inject client controller, HUD script, and HUD ScreenGui.

    Returns the number of scripts/guis added.
    """
    if not detect_fps_game(place):
        return 0

    added = 0

    # Add FPS client controller (only if AI didn't already generate one)
    if not _has_client_fps_controller(place):
        place.scripts.append(generate_fps_client_script())
        added += 1
        log.info("Injected FPS client controller LocalScript")
    else:
        log.info("Skipping FPS controller injection (AI-generated client controller already exists)")

    # Only add HUD ScreenGui if Canvas conversion didn't already create one
    if not _has_hud_screen_gui(place):
        place.screen_guis.append(generate_hud_screen_gui())
        added += 1
        log.info("Injected HUD ScreenGui")
    else:
        log.info("Skipping HUD ScreenGui injection (Canvas-converted HUD already exists)")

    # Add HUD controller LocalScript
    place.scripts.append(generate_hud_client_script())
    added += 1
    log.info("Injected HUD controller LocalScript")

    return added
