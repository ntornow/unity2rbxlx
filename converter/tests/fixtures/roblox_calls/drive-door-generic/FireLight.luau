local STUDS_PER_METER = 3.571

local FireLight = {}
FireLight.__index = FireLight

function FireLight.new(config)
    local self = setmetatable({}, FireLight)
    self.m_Rnd = 0
    self.m_Burning = true
    self.m_Light = nil
    self._t = 0
    return self
end

function FireLight:Start()
    self.m_Rnd = math.random() * 100
    self.m_Light = self:GetComponent("Light")
end

function FireLight:Update(dt)
    self._t = self._t + dt
    if self.m_Burning then
        local t = self._t
        if self.m_Light then
            self.m_Light.Brightness = 2 * math.noise(self.m_Rnd + t, self.m_Rnd + 1 + t * 1)
        end
        local x = math.noise(self.m_Rnd + 0 + t * 2, self.m_Rnd + 1 + t * 2) - 0.5
        local y = math.noise(self.m_Rnd + 2 + t * 2, self.m_Rnd + 3 + t * 2) - 0.5
        local z = math.noise(self.m_Rnd + 4 + t * 2, self.m_Rnd + 5 + t * 2) - 0.5
        local localPos = (Vector3.new(0, 1, 0) + Vector3.new(x, y, z) * 1) * STUDS_PER_METER
        local parent = self.gameObject.Parent
        if parent and parent:IsA("PVInstance") then
            self.gameObject:PivotTo(parent:GetPivot() * CFrame.new(localPos))
        else
            self.gameObject:PivotTo(CFrame.new(localPos))
        end
    end
end

function FireLight:Extinguish()
    self.m_Burning = false
    if self.m_Light then
        self.m_Light.Enabled = false
    end
end

return FireLight