local SpecularLighting = {}
SpecularLighting.__index = SpecularLighting

function SpecularLighting.new(config)
    local self = setmetatable({}, SpecularLighting)
    self.m_WaterBase = nil
    return self
end

function SpecularLighting:Start()
    self.m_WaterBase = self:GetComponent("WaterBase")
end

function SpecularLighting:Update(dt)
    if not self.m_WaterBase then
        self.m_WaterBase = self:GetComponent("WaterBase")
    end

    if self.specularLight and self.m_WaterBase and self.m_WaterBase.sharedMaterial then
        -- UNCONVERTED: sets the "_WorldLightDir" shader vector on the WaterBase's
        -- sharedMaterial to specularLight.transform.forward; Roblox materials expose no
        -- arbitrary shader-vector API, so the directional-specular lighting cue is inert.
    end
end

return SpecularLighting