local Player = {}
Player.__index = Player

local STUDS_PER_METER = 3.571
Player.maxHealth = 100
Player.maxAmmo = 250

function Player.new(config)
    local self = setmetatable({}, Player)
    config = config or {}
    -- Scalars only; never touch self.host / GetComponent here.
    self.speed = config.speed or 6
    self.jumpSpeed = config.jumpSpeed or 1.5
    self.gravity = config.gravity or 0.5
    self.shootRange = config.shootRange or 100
    self.minAngle = config.minAngle or -30
    self.maxAngle = config.maxAngle or 45
    self.sensitivity = config.sensitivity or 200
    self.healAmount = config.healAmount or 25
    self.ammoAmount = config.ammoAmount or 40

    self.curHealth = Player.maxHealth
    self.curAmmo = 0
    self.gotItems = {}
    self.gotWeapon = false
    self.gotKey = false
    self.walkTime = 0
    self.timeScale = 1

    -- Edge-detection state for key/mouse "GetKeyDown"/"GetButtonDown".
    self._keyHeld = {}
    self._fireHeld = false
    self._pauseHeld = false
    return self
end

function Player:Awake()
    -- static GameObject instance = gameObject
    Player.instance = self.gameObject

    self.source = self:GetComponent("AudioSource")
    -- CharacterController + camera are host-owned; movement/aim run through
    -- self.host.player. weaponSlot was cam.GetChild(0); with the camera owned by
    -- the host, fall back to this GameObject as the weapon parent.
    self.weaponSlot = self.gameObject

    -- Static cross-script events: parent to ReplicatedStorage so other scripts can
    -- find and :Connect them. Idempotent.
    Player.HealthUpdate = Player.HealthUpdate or (function() local b = Instance.new("BindableEvent"); b.Name = "HealthUpdate"; b.Parent = game:GetService("ReplicatedStorage"); return b end)()
    Player.AmmoUpdate = Player.AmmoUpdate or (function() local b = Instance.new("BindableEvent"); b.Name = "AmmoUpdate"; b.Parent = game:GetService("ReplicatedStorage"); return b end)()
    Player.ItemUpdate = Player.ItemUpdate or (function() local b = Instance.new("BindableEvent"); b.Name = "ItemUpdate"; b.Parent = game:GetService("ReplicatedStorage"); return b end)()
    Player.PauseEvent = Player.PauseEvent or (function() local b = Instance.new("BindableEvent"); b.Name = "PauseEvent"; b.Parent = game:GetService("ReplicatedStorage"); return b end)()
end

function Player:Update(dt)
    self:Shoot()
    -- Rotate() and Move() are host-owned (camera aim + character movement);
    -- self.host.player drives them. See those methods for details.

    -- Pause toggle. Unity used KeyCode.Escape, which Roblox reserves; remapped to P.
    local UIS = game:GetService("UserInputService")
    local pDown = UIS:IsKeyDown(Enum.KeyCode.P)
    if pDown and not self._pauseHeld then
        local pause = self.timeScale > 0
        if Player.PauseEvent then Player.PauseEvent:Fire(pause) end
    end
    self._pauseHeld = pDown

    -- Dev bypass keys (Unity #if UNITY_EDITOR || DEVELOPMENT_BUILD).
    self:Bypass()
end

function Player:Bypass()
    if self:_keyDown(Enum.KeyCode.F1) then
        self.gotKey = true
        self:_setSharedFlag("hasKey", true)
    elseif self:_keyDown(Enum.KeyCode.F2) then
        self:GetItem("Battery")
    elseif self:_keyDown(Enum.KeyCode.F3) then
        self:GetItem("SmallBattery")
    elseif self:_keyDown(Enum.KeyCode.F4) then
        self:GetItem("MediumBattery")
    elseif self:_keyDown(Enum.KeyCode.F5) then
        self:GetItem("GasCan")
    end
end

-- Controls camera rotation oriented by mouse position.
function Player:Rotate()
    -- Camera aim is host-owned via self.host.player; the host applies mouse look
    -- and clamps pitch. Recoil is applied in Shoot via applyRecoil.
    -- UNCONVERTED: aim is driven by the host, not the player script.
end

-- Controls movement with CharacterController.
function Player:Move()
    -- Movement is host-owned via self.host.player. The walk-sound feedback depended
    -- on host-private grounded/moveDirection state, which is not exposed here.
    -- UNCONVERTED: movement is driven by the host, not the player script.
end

-- Raycasts to the target position, and sends a message to the object if hit.
function Player:Shoot()
    if self.gotWeapon and self:_fireDown() then
        if self.curAmmo > 0 then
            self:_playOneShot(self.shootSound)
            self.curAmmo = self.curAmmo - 1
            if Player.AmmoUpdate then Player.AmmoUpdate:Fire(self.curAmmo) end
            -- camRotation.x -= 2 (recoil pitch kick, in degrees)
            self.host.player:applyRecoil(2)

            -- Muzzle flare at the bullet origin.
            if self.feedbackPrefab and self.bulletOrigin then
                local cf = self.bulletOrigin:GetPivot()
                local gunFlare = self.host.instantiatePrefab(self.feedbackPrefab, self.bulletOrigin, cf)
                if gunFlare then
                    task.delay(0.1, function() self.host.destroy(gunFlare) end)
                end
            end

            -- Hitscan from the camera center (Camera.main center ray).
            local look = self.host.player:getLookCFrame()
            local origin = look.Position
            local dir = look.LookVector
            local params = RaycastParams.new()
            local result = workspace:Raycast(origin, dir.Unit * (self.shootRange * STUDS_PER_METER), params)
            if result then
                -- SendMessage("TakeDamage", DontRequireReceiver): deliver damage to the
                -- hit object's owning model if it carries a damageable Humanoid.
                local model = result.Instance:FindFirstAncestorWhichIsA("Model")
                local hum = model and model:FindFirstChildWhichIsA("Humanoid")
                if hum then hum:TakeDamage(100) end

                if self.feedbackPrefab then
                    local hitFlare = self.host.instantiatePrefab(self.feedbackPrefab, workspace, CFrame.new(result.Position))
                    if hitFlare then
                        task.delay(0.2, function() self.host.destroy(hitFlare) end)
                    end
                end
            end
        else
            self:_playOneShot(self.dryFireSound)
        end
    end
end

-- Updates the player spawnpoint by a given point.
function Player:UpdateSpawnpoint(newPoint)
    self.spawnPoint = newPoint
end

-- Takes a given amount of damage.
function Player:TakeDamage(amount)
    self.curHealth = self.curHealth - amount
    if self.curHealth <= 0 then
        if self.spawnPoint then
            -- Respawn is host-owned: teleport to the spawn transform.
            self.host.player:teleport(self.spawnPoint:GetPivot())
        end
        self.curHealth = Player.maxHealth
    end
    self:_playOneShot(self.hitSound)
    if Player.HealthUpdate then Player.HealthUpdate:Fire(self.curHealth) end
end

-- Recovers the default amount of health.
function Player:RecoverHealth()
    if self.curHealth == Player.maxHealth then
        return
    end

    self.curHealth = self.curHealth + self.healAmount
    if self.curHealth > Player.maxHealth then
        self.curHealth = Player.maxHealth
    end

    self:_playOneShot(self.pickHealthSound)
    if Player.HealthUpdate then Player.HealthUpdate:Fire(self.curHealth) end
end

-- Collects a given amount of ammunition.
function Player:TakeAmmo(amount)
    self.curAmmo = self.curAmmo + amount
    if self.curAmmo > Player.maxAmmo then
        self.curAmmo = Player.maxAmmo
    end

    self:_playOneShot(self.pickAmmoSound)
    if Player.AmmoUpdate then Player.AmmoUpdate:Fire(self.curAmmo) end
end

-- Gets the rifle and equips it.
function Player:GetRifle()
    self:_playOneShot(self.pickAmmoSound)
    if self.riflePrefab then
        local slot = self:_resolveWeaponSlot() or self.gameObject
        local rifle = self.host.instantiatePrefab(self.riflePrefab, slot, slot:GetPivot())
        if rifle then
            -- localScale = Vector3.one * 0.2; localPosition zero; localRotation identity.
            if rifle:IsA("Model") then
                rifle:ScaleTo(0.2)
            end
            rifle:PivotTo(slot:GetPivot())
        end
    end
    self.gotWeapon = true
    self:TakeAmmo(20)
end

-- Gets an item and processes its required logic.
function Player:GetItem(itemName)
    -- Canonical attribute mirror BEFORE the local-field update so cross-script
    -- readers (Door checks hasKey, HUD, etc.) and the server see the pickup.
    if itemName and itemName ~= "" then
        local flag = "has" .. (itemName:gsub("[^%w_]+", "_"))
        self:_setSharedFlag(flag, true)
    end

    if itemName == "Key" then
        self.gotKey = true
        self:_playOneShot(self.pickSound)
    elseif itemName == "Rifle" then
        self:GetRifle()
    elseif itemName == "Health" then
        self:RecoverHealth()
    elseif itemName == "Ammo" then
        self:TakeAmmo(self.ammoAmount)
    else
        table.insert(self.gotItems, itemName)
        self:_playOneShot(self.pickSound)
        if Player.ItemUpdate then Player.ItemUpdate:Fire(itemName) end
    end
end

-- public bool hasKey { get { return gotKey; } }
function Player:getHasKey()
    return self.gotKey
end

-- public List<string> hasItems { get { return gotItems; } }
function Player:getHasItems()
    return self.gotItems
end

-- Writes a shared player flag: canonical Attribute mirror on the Player + Character,
-- plus the authoritative server mirror via the autogen PlayerSetSharedFlag RemoteEvent.
function Player:_setSharedFlag(flag, value)
    local plr = game:GetService("Players").LocalPlayer
    if plr then plr:SetAttribute(flag, value) end
    local char = plr and plr.Character
    if char then char:SetAttribute(flag, value) end
    local rs = game:GetService("ReplicatedStorage")
    local sf = rs:FindFirstChild("PlayerSetSharedFlag")
    if sf then sf:FireServer(flag, value) end
end

-- PlayOneShot(clip, [volume]) with randomized pitch (Random.Range(0.8, 1.2)).
function Player:_playOneShot(soundId, volume)
    if not soundId then return end
    local s = Instance.new("Sound")
    s.SoundId = soundId
    s.Volume = volume or 1
    s.PlaybackSpeed = math.random() * 0.4 + 0.8
    local part = self.gameObject
    if part and part:IsA("BasePart") then
        s.Parent = part
    else
        s.Parent = game:GetService("SoundService")
    end
    s:Play()
    s.Ended:Connect(function() s:Destroy() end)
    task.delay(5, function() if s and s.Parent then s:Destroy() end end)
end

-- Edge-detected key press, emulating Input.GetKeyDown.
function Player:_keyDown(key)
    local UIS = game:GetService("UserInputService")
    local down = UIS:IsKeyDown(key)
    local edge = down and not self._keyHeld[key]
    self._keyHeld[key] = down
    return edge
end

-- Edge-detected fire, emulating Input.GetButtonDown("Fire").
function Player:_fireDown()
    local UIS = game:GetService("UserInputService")
    local down = UIS:IsMouseButtonPressed(Enum.UserInputType.MouseButton1)
    local edge = down and not self._fireHeld
    self._fireHeld = down
    return edge
end

-- _RIG_RETARGET_WeaponSlot (auto-generated: Camera.main child-ref retargeted to the rig; per-instance, lazy-at-use)
function Player:_resolveWeaponSlot()
    -- memoized per-instance; re-resolves if the cached slot was destroyed
    if self._weaponSlotCache and self._weaponSlotCache.Parent then
        return self._weaponSlotCache
    end
    local function findSlot()
        local rig
        for _, m in workspace:GetDescendants() do
            if m:IsA("Model") and m:GetAttribute("_MainCameraRig") then rig = m break end
        end
        return rig and rig:FindFirstChild("WeaponSlot", true)
    end
    local slot = findSlot()
    if not slot then
        -- the rig Model may stream in after this script's Awake runs.
        for _ = 1, 30 do
            task.wait(0.1)
            slot = findSlot()
            if slot then break end
        end
    end
    self._weaponSlotCache = slot
    return slot   -- a REAL Instance (or nil), NOT a table
end

return Player