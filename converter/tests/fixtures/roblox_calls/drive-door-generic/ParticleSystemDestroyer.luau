local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    self.minDuration = config.minDuration or 8
    self.maxDuration = config.maxDuration or 10
    self.m_MaxLifetime = 0
    self.m_EarlyStop = false
    return self
end

function Class:Start()
    -- IEnumerator Start() runs as a lifecycle-scoped coroutine.
    self.host.startCoroutine(self, function()
        -- GetComponentsInChildren<ParticleSystem>()
        local systems = {}
        for _, d in self.gameObject:GetDescendants() do
            if d:IsA("ParticleEmitter") then
                table.insert(systems, d)
            end
        end

        -- find out the maximum lifetime of any particles in this effect
        for _, system in systems do
            -- Roblox ParticleEmitter.Lifetime is a NumberRange; use its Max as startLifetime.constant
            self.m_MaxLifetime = math.max(system.Lifetime.Max, self.m_MaxLifetime)
        end

        -- wait for random duration
        local stopTime = os.clock() + (math.random() * (self.maxDuration - self.minDuration) + self.minDuration)

        while os.clock() < stopTime or self.m_EarlyStop do
            task.wait()
        end
        print("stopping " .. self.gameObject.Name)

        -- turn off emission
        for _, system in systems do
            system.Enabled = false
        end
        -- UNCONVERTED: BroadcastMessage("Extinguish", DontRequireReceiver) — Unity name-dispatch
        -- to all components on this GameObject and its children has no Roblox equivalent.

        -- wait for any remaining particles to expire
        task.wait(self.m_MaxLifetime)

        self.host.destroy(self.gameObject)
    end)
end

function Class:Stop()
    -- stops the particle system early
    self.m_EarlyStop = true
end

return Class