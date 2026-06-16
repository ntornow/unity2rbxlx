local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    self.multiplier = config.multiplier or 1
    self.m_Systems = {}
    return self
end

function Class:Start()
    -- m_Systems = GetComponentsInChildren<ParticleSystem>()
    self.m_Systems = {}
    for _, descendant in self.gameObject:GetDescendants() do
        if descendant:IsA("ParticleEmitter") or descendant:IsA("ParticleSystem") then
            table.insert(self.m_Systems, descendant)
        end
    end
end

function Class:Extinguish()
    for _, system in self.m_Systems do
        -- emission.enabled = false
        system.Enabled = false
    end
end

return Class