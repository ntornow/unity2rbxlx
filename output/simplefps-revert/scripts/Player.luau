-- u2r: disable default PlayerModule controls + assert FPS mouse state + first-person body hide + spawn floor-snap
-- Re-applies on CharacterAdded because Roblox's character spawn flow
-- re-enables the default PlayerModule and resets MouseBehavior, and
-- because Roblox loads avatar accessories asynchronously after the
-- character spawns — DescendantAdded catches each late-added Handle
-- so the user's hat/chain/glasses don't float across the FPS camera.
do
    local _lp = game:GetService("Players").LocalPlayer
    local _UIS = game:GetService("UserInputService")
    local function _applyFpsMouseState()
        local _ps = _lp:WaitForChild("PlayerScripts", 10)
        local _pm = _ps and _ps:WaitForChild("PlayerModule", 10)
        if _pm then
            local ok, mod = pcall(require, _pm)
            if ok and mod then
                local ok2, controls = pcall(function() return mod:GetControls() end)
                if ok2 and controls and controls.Disable then
                    pcall(function() controls:Disable() end)
                end
            end
        end
        _UIS.MouseBehavior = Enum.MouseBehavior.LockCenter
        _UIS.MouseIconEnabled = false
    end
    local function _isInWeaponSlot(inst)
        local p = inst.Parent
        while p and p ~= game do
            if p.Name == "WeaponSlot" then return true end
            p = p.Parent
        end
        return false
    end
    local function _hidePart(part)
        if (part:IsA("BasePart") or part:IsA("Decal")) and not _isInWeaponSlot(part) then
            part.LocalTransparencyModifier = 1
        end
    end
    local function _hideCharacter(char)
        if not char then return end
        char.DescendantAdded:Connect(_hidePart)
        for _, part in char:GetDescendants() do _hidePart(part) end
    end
    local function _snapToFloor(char)
        if not char then return end
        local hrp = char:WaitForChild("HumanoidRootPart", 5)
        if not hrp then return end
        task.wait()
        local rp = RaycastParams.new()
        rp.FilterDescendantsInstances = {char}
        rp.FilterType = Enum.RaycastFilterType.Exclude
        local hit = workspace:Raycast(hrp.Position, Vector3.new(0, -200, 0), rp)
        if not hit then return end
        local target = hit.Position + Vector3.new(0, 3, 0)
        if (hrp.Position - target).Magnitude > 2 then
            hrp.CFrame = hrp.CFrame + (target - hrp.Position)
        end
    end
    _applyFpsMouseState()
    _hideCharacter(_lp.Character)
    _snapToFloor(_lp.Character)
    _lp.CharacterAdded:Connect(function(char)
        task.wait()
        _applyFpsMouseState()
        _hideCharacter(char)
        _snapToFloor(char)
    end)
end

-- Services
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Debris = game:GetService("Debris")

-- Static constants
local _maxHealth = 100
local _maxAmmo = 250

-- Cross-script events (BindableEvents parented to script so other
-- LocalScripts can :WaitForChild them and Connect to .Event).
local function makeEvent(name)
    local e = Instance.new("BindableEvent")
    e.Name = name
    e.Parent = script
    return e
end
local healthUpdateEvent = makeEvent("HealthUpdate")
local ammoUpdateEvent = makeEvent("AmmoUpdate")
local itemUpdateEvent = makeEvent("ItemUpdate")
local pauseEvent = makeEvent("PauseEvent")

-- Audio
local function createSound(soundId)
    local s = Instance.new("Sound")
    s.SoundId = soundId
    s.Parent = script
    return s
end
local walkSound = createSound("rbxassetid://89370470082676")
local shootSound = createSound("rbxassetid://79924534148926")
local dryFireSound = createSound("rbxassetid://114051454658537")
local hitSound = createSound("rbxassetid://75411340630488")
local pickAmmoSound = createSound("rbxassetid://81935925926691")
local pickHealthSound = createSound("rbxassetid://132611916517910")
local pickSound = createSound("rbxassetid://118209857531386")

-- Configuration (from serialized fields)
local speed = 6
local jumpSpeed = 1.5
local gravity = 0.5
local shootRange = 100
local minAngle = -30
local maxAngle = 45
local sensitivity = 200
local healAmount = 25
local ammoAmount = 40

-- References
local localPlayer = Players.LocalPlayer
local camera = workspace.CurrentCamera

-- State
local character = nil
local humanoid = nil
local rootPart = nil
local curHealth = _maxHealth
local curAmmo = 0
local gotItems = {}
local gotWeapon = false
local _fpsRifle = nil  -- _FPS_RIFLE_SYSTEM
local _fpsRiflePrimary = nil
local gotKey = false
local walkTime = 0
local spawnPoint = nil
local yawAngle = 0
local pitchAngle = 0
local weaponSlot = nil
local currentRifle = nil
-- Mirrors Unity weaponSlot transform (Player.prefab, child of Main Camera):
-- localPosition (0.4, -0.64, 0.6) METERS, localRotation identity.
-- The Roblox converter scales all geometry from Unity meters to Roblox
-- studs via STUDS_PER_METER (3.571), so the offset must use the same
-- conversion to stay consistent with the rifle template's part positions.
-- Z is also negated because Roblox CFrame uses -Z as the local-forward
-- axis whereas Unity uses +Z forward.
local STUDS_PER_METER = 3.571
local rifleOffset = CFrame.new(
    0.4 * STUDS_PER_METER,
    -0.64 * STUDS_PER_METER,
    -0.6 * STUDS_PER_METER
)
local paused = false

-- Mirror shared state onto LocalPlayer attributes for cross-script
-- consumers (Door checks hasKey, Pickup checks hasItems, etc.). LocalScripts
-- cannot be require()d, so attributes are the only client-side IPC channel.
local function setHasKey(value)
    gotKey = value
    localPlayer:SetAttribute("hasKey", value)
end
local function pushItemAttribute(itemName)
    -- Concatenate items into a comma-separated attribute so other
    -- scripts can string-find.
    local current = localPlayer:GetAttribute("hasItems") or ""
    if current == "" then
        localPlayer:SetAttribute("hasItems", itemName)
    else
        localPlayer:SetAttribute("hasItems", current .. "," .. itemName)
    end
end

setHasKey(false)
localPlayer:SetAttribute("hasItems", "")
localPlayer:SetAttribute("curHealth", curHealth)
localPlayer:SetAttribute("curAmmo", curAmmo)
localPlayer:SetAttribute("maxHealth", _maxHealth)
localPlayer:SetAttribute("maxAmmo", _maxAmmo)

-- Forward declarations
local takeDamage
local recoverHealth
local takeAmmo
local getRifle
local getItem

-- Character setup
local function onCharacterAdded(char)
    character = char
    humanoid = char:WaitForChild("Humanoid")
    rootPart = char:WaitForChild("HumanoidRootPart")
    humanoid.WalkSpeed = speed * 3
    humanoid.JumpPower = jumpSpeed * 30

    -- weaponSlot in Unity was the camera's first child. In Roblox we
    -- attach to the character's Head so it follows the FPS camera pose.
    local head = char:WaitForChild("Head")
    local existing = head:FindFirstChild("WeaponSlot")
    if existing then existing:Destroy() end
    local attach = Instance.new("Attachment")
    attach.Name = "WeaponSlot"
    attach.Position = Vector3.new(0.5, -0.3, -1)
    attach.Parent = head
    weaponSlot = attach

    curHealth = _maxHealth
    localPlayer:SetAttribute("curHealth", curHealth)
    healthUpdateEvent:Fire(curHealth)
    ammoUpdateEvent:Fire(curAmmo)
end

localPlayer.CharacterAdded:Connect(onCharacterAdded)
if localPlayer.Character then
    task.spawn(onCharacterAdded, localPlayer.Character)
end

-- FPS camera
camera.CameraType = Enum.CameraType.Scriptable
UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
UserInputService.MouseIconEnabled = false

local function rotate(dt)
    local mouseDelta = UserInputService:GetMouseDelta()
    yawAngle = yawAngle - mouseDelta.X * sensitivity * dt * 0.005
    pitchAngle = pitchAngle - mouseDelta.Y * sensitivity * dt * 0.005
    pitchAngle = math.clamp(pitchAngle, math.rad(minAngle), math.rad(maxAngle))
end

local function move(dt)
    if not humanoid or not rootPart then return end

    local moveX = 0
    local moveZ = 0
    if UserInputService:IsKeyDown(Enum.KeyCode.D) then moveX = moveX + 1 end
    if UserInputService:IsKeyDown(Enum.KeyCode.A) then moveX = moveX - 1 end
    if UserInputService:IsKeyDown(Enum.KeyCode.W) then moveZ = moveZ - 1 end
    if UserInputService:IsKeyDown(Enum.KeyCode.S) then moveZ = moveZ + 1 end

    local inputDir = Vector3.new(moveX, 0, moveZ)
    if inputDir.Magnitude > 0 then inputDir = inputDir.Unit end
    local moveDir = (CFrame.Angles(0, yawAngle, 0) * inputDir)
    humanoid:Move(moveDir, false)

    if moveDir.Magnitude > 0.3 and walkTime > 0.4 then
        walkTime = 0
        walkSound.PlaybackSpeed = math.random() * 0.4 + 0.8
        walkSound.Volume = 0.3
        walkSound:Play()
    end
    walkTime = walkTime + dt
end

local function shoot()
    if not gotWeapon then return end

    if curAmmo > 0 then
        shootSound.PlaybackSpeed = math.random() * 0.4 + 0.8
        shootSound:Play()
        curAmmo = curAmmo - 1
        localPlayer:SetAttribute("curAmmo", curAmmo)
        ammoUpdateEvent:Fire(curAmmo)
        pitchAngle = pitchAngle - math.rad(2)

        local templates = ReplicatedStorage:FindFirstChild("Templates")
        local feedbackPrefab = templates and templates:FindFirstChild("Flare")
        local originPos = camera.CFrame.Position

        if feedbackPrefab and weaponSlot then
            local gunFlare = feedbackPrefab:Clone()

                -- Fix clone visibility and weld sub-mesh parts together
                local _primary
                if gunFlare:IsA("BasePart") then
                    _primary = gunFlare
                    gunFlare.Transparency = 0
                    gunFlare.Anchored = false
                    gunFlare.CanCollide = false
                elseif gunFlare:IsA("Model") then
                    _primary = gunFlare.PrimaryPart or gunFlare:FindFirstChildWhichIsA("BasePart")
                end
                for _, _p in gunFlare:GetDescendants() do
                    if _p:IsA("BasePart") then
                        _p.Transparency = 0
                        _p.Anchored = false
                        _p.CanCollide = false
                        if _primary and _p ~= _primary then
                            local _w = Instance.new("WeldConstraint")
                            _w.Part0 = _p
                            _w.Part1 = _primary
                            _w.Parent = _p
                        end
                    end
                end
            if gunFlare:IsA("Model") then
                if not gunFlare.PrimaryPart then
                    gunFlare.PrimaryPart = gunFlare:FindFirstChildWhichIsA("BasePart")
                end
                gunFlare:PivotTo(CFrame.new(weaponSlot.WorldPosition))
            elseif gunFlare:IsA("BasePart") then
                gunFlare.CFrame = CFrame.new(weaponSlot.WorldPosition)
            end
            gunFlare.Parent = weaponSlot.Parent
            Debris:AddItem(gunFlare, 0.1)
        end

        local raycastParams = RaycastParams.new()
        if character then
            raycastParams.FilterDescendantsInstances = {character}
            raycastParams.FilterType = Enum.RaycastFilterType.Exclude
        end
        local direction = camera.CFrame.LookVector * shootRange
        local result = workspace:Raycast(originPos, direction, raycastParams)
        if result then
            local hit = result.Instance
            -- SendMessage("TakeDamage") in Unity dispatches to any listener
            -- on the collider's GameObject hierarchy. Roblox equivalent:
            -- toggle a TakeDamage attribute on the hit Part and its Model
            -- parent — receivers connect via GetAttributeChangedSignal.
            hit:SetAttribute("TakeDamage", (hit:GetAttribute("TakeDamage") or 0) + 1)

            -- _AutoDamageRemoteEventInjected: mirror client damage to server
            do
                local _de = workspace:FindFirstChild("DamageEvent", true) or workspace:FindFirstChild("damageevent", true) or workspace:FindFirstChild("damageEvent", true)
                if _de then
                    local _cam = workspace.CurrentCamera
                    -- Send the value the client just wrote so
                    -- the server preserves it verbatim (codex
                    -- round-10 [P1]: don't let the server
                    -- synthesize a counter that overwrites the
                    -- client's damage payload).
                    local _td = hit:GetAttribute("TakeDamage")
                    _de:FireServer(hit, _td, _cam.CFrame.Position, _cam.CFrame.LookVector)
                end
            end
            local parentModel = hit:FindFirstAncestorOfClass("Model")
            if parentModel then
                parentModel:SetAttribute("TakeDamage", (parentModel:GetAttribute("TakeDamage") or 0) + 1)
            end
            if feedbackPrefab then
                local hitFlare = feedbackPrefab:Clone()

                    -- Fix clone visibility and weld sub-mesh parts together
                    local _primary
                    if hitFlare:IsA("BasePart") then
                        _primary = hitFlare
                        hitFlare.Transparency = 0
                        hitFlare.Anchored = false
                        hitFlare.CanCollide = false
                    elseif hitFlare:IsA("Model") then
                        _primary = hitFlare.PrimaryPart or hitFlare:FindFirstChildWhichIsA("BasePart")
                    end
                    for _, _p in hitFlare:GetDescendants() do
                        if _p:IsA("BasePart") then
                            _p.Transparency = 0
                            _p.Anchored = false
                            _p.CanCollide = false
                            if _primary and _p ~= _primary then
                                local _w = Instance.new("WeldConstraint")
                                _w.Part0 = _p
                                _w.Part1 = _primary
                                _w.Parent = _p
                            end
                        end
                    end
                if hitFlare:IsA("Model") then
                    if not hitFlare.PrimaryPart then
                        hitFlare.PrimaryPart = hitFlare:FindFirstChildWhichIsA("BasePart")
                    end
                    hitFlare:PivotTo(CFrame.new(result.Position))
                elseif hitFlare:IsA("BasePart") then
                    hitFlare.CFrame = CFrame.new(result.Position)
                end
                hitFlare.Parent = workspace
                Debris:AddItem(hitFlare, 0.2)
            end
        end
    else
        dryFireSound.PlaybackSpeed = math.random() * 0.4 + 0.8
        dryFireSound:Play()
    end
end

local function updateSpawnpoint(newPoint)
    spawnPoint = newPoint
end

takeDamage = function(amount)
    curHealth = curHealth - amount
    if curHealth <= 0 then
        if spawnPoint and rootPart then
            local cf = nil
            if typeof(spawnPoint) == "Instance" then
                if spawnPoint:IsA("BasePart") then
                    cf = spawnPoint.CFrame
                elseif spawnPoint:IsA("Model") then
                    cf = spawnPoint:GetPivot()
                end
            elseif typeof(spawnPoint) == "CFrame" then
                cf = spawnPoint
            end
            if cf then
                rootPart.CFrame = cf
            end
        end
        curHealth = _maxHealth
    end
    localPlayer:SetAttribute("curHealth", curHealth)
    hitSound.PlaybackSpeed = math.random() * 0.4 + 0.8
    hitSound:Play()
    healthUpdateEvent:Fire(curHealth)
end

recoverHealth = function()
    if curHealth == _maxHealth then return end
    curHealth = curHealth + healAmount
    if curHealth > _maxHealth then curHealth = _maxHealth end
    localPlayer:SetAttribute("curHealth", curHealth)
    pickHealthSound.PlaybackSpeed = math.random() * 0.4 + 0.8
    pickHealthSound:Play()
    healthUpdateEvent:Fire(curHealth)
end

takeAmmo = function(amount)
    curAmmo = curAmmo + amount
    if curAmmo > _maxAmmo then curAmmo = _maxAmmo end
    localPlayer:SetAttribute("curAmmo", curAmmo)
    pickAmmoSound.PlaybackSpeed = math.random() * 0.4 + 0.8
    pickAmmoSound:Play()
    ammoUpdateEvent:Fire(curAmmo)
end

getRifle = function()
    pickAmmoSound.PlaybackSpeed = math.random() * 0.4 + 0.8
    pickAmmoSound:Play()
    local templates = ReplicatedStorage:FindFirstChild("Templates")
    local riflePrefab = templates and templates:FindFirstChild("Rifle")
    if riflePrefab and weaponSlot then
        if currentRifle then
            currentRifle:Destroy()
            currentRifle = nil
        end
        local rifle = riflePrefab:Clone()
        -- Do NOT override PrimaryPart. Unity's GetRifle parents the prefab
        -- under weaponSlot with localPosition zero, so the rifle's
        -- WORLDPIVOT (= prefab root transform) ends up at weaponSlot. The
        -- Roblox Rifle template's WorldPivot already corresponds to the
        -- Unity prefab root, so :PivotTo on the Model (with PrimaryPart
        -- unset) honors that pivot. Setting PrimaryPart to any part would
        -- repivot the model around that part, breaking the Unity mapping.
        -- Parts must be Anchored so gravity doesn't drop them — the
        -- per-frame :PivotTo below drives the pose directly. Welds aren't
        -- needed once the parts ride the Model's pivot.
        for _, p in ipairs(rifle:GetDescendants()) do
            if p:IsA("BasePart") then
                p.Transparency = 0
                p.CanCollide = false
                p.Massless = true
                p.Anchored = true
            end
        end
        if rifle:IsA("Model") then
            rifle:ScaleTo(0.2)
            rifle:PivotTo(camera.CFrame * rifleOffset)
        elseif rifle:IsA("BasePart") then
            rifle.Size = rifle.Size * 0.2
            rifle.CFrame = camera.CFrame * rifleOffset
        end
        -- Parent to workspace, not the character. Rifle parts under a
        -- character descendant get LocalTransparencyModifier = 1 written
        -- every frame by the built-in PlayerModule.CameraModule (first-
        -- person body hide); and parts parented under an Attachment do
        -- not render. Workspace sidesteps both.
        rifle.Parent = workspace
        currentRifle = rifle
    end
    gotWeapon = true
    localPlayer:SetAttribute("gotWeapon", true)
    takeAmmo(20)
end

getItem = function(itemName)
    if itemName == "Key" then
        setHasKey(true)
        pickSound.PlaybackSpeed = math.random() * 0.4 + 0.8
        pickSound:Play()
    elseif itemName == "Rifle" then
        getRifle()
    elseif itemName == "Health" then
        recoverHealth()
    elseif itemName == "Ammo" then
        takeAmmo(ammoAmount)
    else
        table.insert(gotItems, itemName)
        pushItemAttribute(itemName)
        pickSound.PlaybackSpeed = math.random() * 0.4 + 0.8
        pickSound:Play()
        itemUpdateEvent:Fire(itemName)
    end
end

-- Input handlers
UserInputService.InputBegan:Connect(function(input, processed)
    if processed then return end
    if input.UserInputType == Enum.UserInputType.MouseButton1 then
        shoot()
    elseif input.KeyCode == Enum.KeyCode.Space then
        if humanoid then humanoid.Jump = true end
    elseif input.KeyCode == Enum.KeyCode.P then
        -- Replaced KeyCode.Escape (reserved by Roblox for the system menu)
        paused = not paused
        pauseEvent:Fire(paused)
    elseif input.KeyCode == Enum.KeyCode.F1 then
        setHasKey(true)
    elseif input.KeyCode == Enum.KeyCode.F2 then
        getItem("Battery")
    elseif input.KeyCode == Enum.KeyCode.F3 then
        getItem("SmallBattery")
    elseif input.KeyCode == Enum.KeyCode.F4 then
        getItem("MediumBattery")
    elseif input.KeyCode == Enum.KeyCode.F5 then
        getItem("GasCan")
    end
end)

-- Receive TakeDamage requests on the LocalPlayer (analogue of Unity's
-- SendMessage("TakeDamage", amount)).
localPlayer:GetAttributeChangedSignal("TakeDamage"):Connect(function()
    local amount = localPlayer:GetAttribute("TakeDamage")
    if typeof(amount) == "number" and amount > 0 then
        takeDamage(amount)
        localPlayer:SetAttribute("TakeDamage", 0)
    end
end)

-- Cross-script callable: other LocalScripts can fire an UpdateSpawnpoint
-- BindableEvent child of this script to change the respawn target.
local spawnEvent = makeEvent("UpdateSpawnpoint")
spawnEvent.Event:Connect(updateSpawnpoint)

-- Update loop
RunService.Heartbeat:Connect(function(dt)
    if not character then return end
    rotate(dt)
    move(dt)

    local head = character:FindFirstChild("Head")
    if head then
        local headPos = head.Position + Vector3.new(0, 0.5, 0)
        camera.CFrame = CFrame.new(headPos) * CFrame.Angles(0, yawAngle, 0) * CFrame.Angles(pitchAngle, 0, 0)
    end
    -- Drive the rifle pose from the camera so it tracks yaw+pitch.
    if currentRifle and currentRifle.Parent then
        if currentRifle:IsA("Model") then
            currentRifle:PivotTo(camera.CFrame * rifleOffset)
        elseif currentRifle:IsA("BasePart") then
            currentRifle.CFrame = camera.CFrame * rifleOffset
        end
    end
end)

-- Pickup via RemoteEvent (server fires when player touches pickup)
local _pickupEvt = game:GetService("ReplicatedStorage"):WaitForChild("PickupItemEvent", 5)
if _pickupEvt then
    _pickupEvt.OnClientEvent:Connect(function(itemName)
        if itemName and itemName ~= "" then getItem(itemName) end
    end)
end

-- Client-side pickup detection
if character then
    for _, part in character:GetChildren() do
        if part:IsA("BasePart") then
            part.Touched:Connect(function(other)
                local pm = other:FindFirstAncestorOfClass("Model")
                if pm and (pm.Name:lower():find("pickup") or pm:FindFirstChild("Pickup")) then
                    local sc = pm:FindFirstChild("Pickup") or pm:FindFirstChildWhichIsA("Script")
                    local iname = (sc and sc:GetAttribute("itemName"))
                        or pm:GetAttribute("itemName") or ""
                    if iname == "" and pm.Name:lower():find("rifle") then iname = "Rifle" end
                    if iname == "" and pm.Name:lower():find("key") then iname = "Key" end
                    if iname == "" and pm.Name:lower():find("ammo") then iname = "Ammo" end
                    if iname == "" and (pm.Name:lower():find("health") or pm.Name:lower():find("hp")) then iname = "Health" end
                    if iname ~= "" then getItem(iname); pm:Destroy() end
                end
            end)
        end
    end
end
