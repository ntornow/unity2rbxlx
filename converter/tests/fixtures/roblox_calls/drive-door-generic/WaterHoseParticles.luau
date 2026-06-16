local WaterHoseParticles = {}
WaterHoseParticles.__index = WaterHoseParticles

WaterHoseParticles.lastSoundTime = 0

function WaterHoseParticles.new(config)
    local self = setmetatable({}, WaterHoseParticles)
    self.force = config.force or 1
    self.m_CollisionEvents = {}
    return self
end

function WaterHoseParticles:Start()
    self.m_ParticleSystem = self:GetComponent("ParticleSystem")
end

function WaterHoseParticles:OnParticleCollision(other)
    -- UNCONVERTED: Roblox has no ParticleSystem collision-event dispatch; Unity's OnParticleCollision and ParticleSystem.GetCollisionEvents (per-particle collider/velocity events) have no Roblox equivalent, so the per-collision force application and "Extinguish" broadcast cannot be faithfully reproduced.
end

return WaterHoseParticles