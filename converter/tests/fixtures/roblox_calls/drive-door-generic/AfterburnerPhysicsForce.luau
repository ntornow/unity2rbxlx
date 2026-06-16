local STUDS_PER_METER = 3.571

local function inverseLerp(a, b, value)
    if a == b then
        return 0
    end
    local t = (value - a) / (b - a)
    if t < 0 then
        return 0
    elseif t > 1 then
        return 1
    end
    return t
end

local AfterburnerPhysicsForce = {}
AfterburnerPhysicsForce.__index = AfterburnerPhysicsForce

function AfterburnerPhysicsForce.new(config)
    local self = setmetatable({}, AfterburnerPhysicsForce)
    self.effectAngle = config.effectAngle or 15
    self.effectWidth = config.effectWidth or 1
    self.effectDistance = config.effectDistance or 10
    self.force = config.force or 10
    self.m_Cols = {}
    self.m_Sphere = nil
    return self
end

function AfterburnerPhysicsForce:OnEnable()
    self.m_Sphere = self:GetComponent("SphereCollider")
end

function AfterburnerPhysicsForce:FixedUpdate(dt)
    if self.m_Sphere == nil then
        return
    end

    local pivot = self.gameObject:GetPivot()
    local center = self.m_Sphere.center or Vector3.new(0, 0, 0)
    local origin = pivot.Position + center
    self.m_Cols = workspace:GetPartBoundsInRadius(origin, self.m_Sphere.radius)

    for n = 1, #self.m_Cols do
        local part = self.m_Cols[n]
        -- attachedRigidbody != null: in Roblox a part participates in physics when not anchored.
        if not part.Anchored then
            local localPos = pivot:PointToObjectSpace(part.Position)

            -- Vector3.MoveTowards(localPos, new Vector3(0, 0, localPos.z), effectWidth * 0.5)
            local target = Vector3.new(0, 0, localPos.Z)
            local maxDist = self.effectWidth * 0.5 * STUDS_PER_METER
            local toTarget = target - localPos
            local dist = toTarget.Magnitude
            if dist <= maxDist or dist == 0 then
                localPos = target
            else
                localPos = localPos + toTarget.Unit * maxDist
            end

            local angle = math.abs(math.deg(math.atan2(localPos.X, localPos.Z)))
            local falloff = inverseLerp(self.effectDistance * STUDS_PER_METER, 0, localPos.Magnitude)
            falloff = falloff * inverseLerp(self.effectAngle, 0, angle)

            local delta = part.Position - pivot.Position
            local impulse = delta.Unit * self.force * falloff
            local applyAt = part.Position + (pivot:PointToWorldSpace(Vector3.new(0, 0, localPos.Z)) - part.Position) * 0.1
            part:ApplyImpulseAtPosition(impulse, applyAt)
        end
    end
end

function AfterburnerPhysicsForce:OnDrawGizmosSelected()
    -- UNCONVERTED: editor-only gizmo rendering (Gizmos.* has no Roblox runtime equivalent)
end

return AfterburnerPhysicsForce