local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    return self
end

function Class:Start()
    local source = self:GetComponent("AudioSource")
    if source and self.extinguishSounds and #self.extinguishSounds > 0 then
        source.SoundId = self.extinguishSounds[math.random(1, #self.extinguishSounds)]
        source:Play()
    end
end

return Class