local Hose = {}
Hose.__index = Hose

function Hose.new(config)
    local self = setmetatable({}, Hose)
    self.maxPower = config.maxPower or 20
    self.minPower = config.minPower or 5
    self.changeSpeed = config.changeSpeed or 5
    self.m_Power = 0
    return self
end

function Hose:Update(dt)
    local UserInputService = game:GetService("UserInputService")

    local target
    if UserInputService:IsMouseButtonPressed(Enum.UserInputType.MouseButton1) then
        target = self.maxPower
    else
        target = self.minPower
    end
    -- Mathf.Lerp(m_Power, target, Time.deltaTime * changeSpeed)
    local t = dt * self.changeSpeed
    self.m_Power = self.m_Power + (target - self.m_Power) * t

    if UserInputService:IsKeyDown(Enum.KeyCode.One) then
        if self.systemRenderer then
            self.systemRenderer.enabled = not self.systemRenderer.enabled
        end
    end

    if self.hoseWaterSystems then
        for _, system in self.hoseWaterSystems do
            -- ParticleSystem.main.startSpeed → ParticleEmitter.Speed
            system.Speed = NumberRange.new(self.m_Power)
            -- emission.enabled
            system.Enabled = (self.m_Power > self.minPower * 1.1)
        end
    end
end

return Hose