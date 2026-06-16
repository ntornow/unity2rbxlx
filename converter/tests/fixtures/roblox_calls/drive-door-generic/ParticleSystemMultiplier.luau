local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    self.multiplier = config.multiplier or 1
    return self
end

local function scaleNumberRange(range, factor)
    return NumberRange.new(range.Min * factor, range.Max * factor)
end

local function scaleNumberSequence(seq, factor)
    local keypoints = {}
    for _, kp in seq.Keypoints do
        table.insert(keypoints, NumberSequenceKeypoint.new(kp.Time, kp.Value * factor, kp.Envelope * factor))
    end
    return NumberSequence.new(keypoints)
end

function Class:Start()
    -- a simple script to scale the size, speed and lifetime of a particle system
    local multiplier = self.multiplier
    local lifetimeMultiplier = multiplier + (1 - multiplier) * 0.5  -- Mathf.Lerp(multiplier, 1, 0.5)

    -- GetComponentsInChildren<ParticleSystem>(): walk descendants and filter
    local systems = {}
    for _, desc in self.gameObject:GetDescendants() do
        if desc:IsA("ParticleEmitter") then
            table.insert(systems, desc)
        end
    end

    for _, system in systems do
        -- startSizeMultiplier
        system.Size = scaleNumberSequence(system.Size, multiplier)
        -- startSpeedMultiplier
        system.Speed = scaleNumberRange(system.Speed, multiplier)
        -- startLifetimeMultiplier
        system.Lifetime = scaleNumberRange(system.Lifetime, lifetimeMultiplier)
        system:Clear()
        system.Enabled = true
    end
end

return Class