local State = { Default = 0, Engaged = 1, Search = 2 }

local STUDS_PER_METER = 3.571

local Turret = {}
Turret.__index = Turret

function Turret.new(config)
    local self = setmetatable({}, Turret)
    -- Scalar config only; host surface is not bound yet (rule e).
    if config.rotate ~= nil then
        self.rotate = config.rotate
    else
        self.rotate = true
    end
    self.rotationSpeed = config.rotationSpeed or 125
    self.engagedSpeed = config.engagedSpeed or 2.5
    self.shootCooldown = config.shootCooldown or 1
    self.maxHealth = config.maxHealth or 5
    self.searchTime = config.searchTime or 3
    return self
end

-- Computed property getters from the C# source.
function Turret:_source()
    return self:GetComponent("AudioSource")
end

function Turret:_tBase()
    return self.gameObject:FindFirstChild("Base")
end

function Turret:_tWeapon()
    local base = self:_tBase()
    return base and base:FindFirstChild("Weapon")
end

function Turret:_tOrigin()
    local weapon = self:_tWeapon()
    return weapon and weapon:FindFirstChild("Origin")
end

function Turret:_sightRadius()
    -- Unity: transform.Find("Collider").GetComponent<SphereCollider>().radius (metres) -> studs.
    local col = self.gameObject:FindFirstChild("Collider")
    if col then
        local r = col:GetAttribute("radius")
        if r then
            return r * STUDS_PER_METER
        end
        if col:IsA("BasePart") then
            return math.max(col.Size.X, col.Size.Y, col.Size.Z) * 0.5
        end
    end
    return 10 * STUDS_PER_METER
end

function Turret:Awake()
    -- OnTriggerStay(other)
    self.host:connectGameObjectSignalStay(self.gameObject, function(other)
        if self.state == State.Engaged then
            return
        end
        local plr = self.host.playerFromTouch(other)
        if not plr then
            return
        end
        local char = plr.Character
        if not char then
            return
        end
        local base = self:_tBase()
        if not base then
            return
        end
        local baseCF = base:GetPivot()
        local basePos = baseCF.Position
        local dir = char:GetPivot().Position - basePos
        if dir.Magnitude <= 0 then
            return
        end
        -- Vector3.Angle(dir, tBase.forward)
        local angle = math.deg(math.acos(math.clamp(dir.Unit:Dot(baseCF.LookVector), -1, 1)))
        if angle < 55 then
            local params = RaycastParams.new()
            params.FilterType = Enum.RaycastFilterType.Exclude
            params.FilterDescendantsInstances = { self.gameObject }
            local result = workspace:Raycast(basePos, dir.Unit * self:_sightRadius(), params)
            if result and result.Instance then
                local hitPlr = self.host.playerFromTouch(result.Instance)
                if hitPlr and hitPlr.Character then
                    local target = hitPlr.Character
                    self.host.startCoroutine(self, function()
                        self:_engagedUpdate(target)
                    end)
                end
            end
        end
    end)

    -- OnTriggerExit(other)
    self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", function(other)
        if self.state == State.Engaged then
            local plr = self.host.playerFromTouch(other)
            if plr then
                self.host.startCoroutine(self, function()
                    self:_searchUpdate()
                end)
            end
        end
    end)
end

function Turret:Start()
    self.health = self.maxHealth
    -- Random.Range(0, 1f) > 0.5f ? Vector3.up : Vector3.down
    if math.random() > 0.5 then
        self.rotateDir = Vector3.new(0, 1, 0)
    else
        self.rotateDir = Vector3.new(0, -1, 0)
    end
    self.host.startCoroutine(self, function()
        self:_defaultUpdate()
    end)
end

-- Decrements health when damage taken and handles death event.
function Turret:TakeDamage()
    self.health = self.health - 1
    local src = self:_source()
    if src then
        -- source.pitch = Random.Range(0.8f, 1.2f)
        src.PlaybackSpeed = math.random() * (1.2 - 0.8) + 0.8
        -- source.PlayOneShot(hitSound)
        if self.hitSound then
            src.SoundId = self.hitSound
            src:Play()
        end
    end
    if self.health <= 0 then
        local base = self:_tBase()
        local weapon = self:_tWeapon()
        local spawnPos
        if base then
            spawnPos = base:GetPivot().Position
        else
            spawnPos = self.gameObject:GetPivot().Position
        end
        -- Instantiate(explosionPrefab/smokePrefab, tBase.position, Quaternion.identity)
        self.host.instantiatePrefab(self.explosionPrefab, workspace, CFrame.new(spawnPos))
        self.host.instantiatePrefab(self.smokePrefab, workspace, CFrame.new(spawnPos))
        if weapon then
            self.host.destroy(weapon)
        end
        if base then
            self.host.destroy(base)
        end
        self.enabled = false
        self.host.destroy(self)
    end
end

-- Continuously rotates the turret to search for targets.
function Turret:_defaultUpdate()
    self.state = State.Default
    while self.state == State.Default do
        local dt = task.wait()
        if self.rotate then
            local base = self:_tBase()
            if base then
                -- tBase.Rotate(rotateDir, dt * rotationSpeed) (local-space, degrees)
                local deg = dt * self.rotationSpeed
                base:PivotTo(base:GetPivot() * CFrame.fromAxisAngle(self.rotateDir, math.rad(deg)))
            end
        end
    end
end

-- Rotates the turret aiming for the target.
function Turret:_engagedUpdate(target)
    self.state = State.Engaged
    self.host.startCoroutine(self, function()
        self:_engagedShooting()
    end)
    while self.state == State.Engaged do
        local dt = task.wait()
        if not (target and target.Parent) then
            break
        end
        local base = self:_tBase()
        local weapon = self:_tWeapon()
        if base and weapon then
            local baseCF = base:GetPivot()
            local basePos = baseCF.Position
            local targetPos = target:GetPivot().Position
            local dir = targetPos - basePos
            if dir.Magnitude > 0 then
                -- tBase.rotation = Quaternion.Lerp(tBase.rotation, LookRotation(dir), dt * engagedSpeed)
                local goal = CFrame.lookAt(basePos, basePos + dir)
                base:PivotTo(baseCF:Lerp(goal, dt * self.engagedSpeed))
            end
            -- tWeapon.LookAt(target) then clamp local pitch to [-30, 30], zero yaw/roll.
            local newBaseCF = base:GetPivot()
            local weaponPos = weapon:GetPivot().Position
            local localDir = newBaseCF:VectorToObjectSpace(targetPos - weaponPos)
            local pitch = math.deg(math.atan2(localDir.Y, -localDir.Z))
            if pitch > 180 then
                pitch = pitch - 360
            end
            pitch = math.clamp(pitch, -30, 30)
            weapon:PivotTo(newBaseCF * CFrame.Angles(math.rad(pitch), 0, 0))
        end
    end
end

-- Shoots at the target in timed intervals.
function Turret:_engagedShooting()
    task.wait(self.shootCooldown)
    while self.state == State.Engaged do
        local origin = self:_tOrigin()
        if origin then
            -- Instantiate(turretBullet, tOrigin.position, tOrigin.rotation)
            self.host.instantiatePrefab(self.turretBullet, workspace, origin:GetPivot())
        end
        local src = self:_source()
        if src then
            src.PlaybackSpeed = math.random() * (1.2 - 0.8) + 0.8
            src:Play()
        end
        task.wait(self.shootCooldown)
    end
end

-- Keeps position waiting for a potential target.
function Turret:_searchUpdate()
    self.state = State.Search
    local time = 0
    while self.state == State.Search and time < self.searchTime do
        local dt = task.wait()
        time = time + dt
    end
    if self.state == State.Search then
        self.host.startCoroutine(self, function()
            self:_defaultUpdate()
        end)
    end
end

return Turret