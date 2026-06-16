local STUDS_PER_METER = 3.571

local CountdownSound = {}
CountdownSound.__index = CountdownSound

local k_StartDelay = 0.5

function CountdownSound.new(config)
    local self = setmetatable({}, CountdownSound)
    self.m_TimeToDisable = 0
    return self
end

function CountdownSound:OnEnable()
    self.m_Source = self:GetComponent("AudioSource")
    self.m_TimeToDisable = self.m_Source.TimeLength
    task.delay(k_StartDelay, function()
        if self.m_Source then
            self.m_Source:Play()
        end
    end)
end

function CountdownSound:Update(dt)
    self.m_TimeToDisable = self.m_TimeToDisable - dt

    if self.m_TimeToDisable < 0 then
        self.host.setActive(self.gameObject, false)
    end
end

return CountdownSound