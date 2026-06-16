local WaterBasic = {}
WaterBasic.__index = WaterBasic

function WaterBasic.new(config)
    local self = setmetatable({}, WaterBasic)
    return self
end

function WaterBasic:Update(dt)
    -- UNCONVERTED: reads WaveSpeed/_WaveScale and writes the _WaveOffset shader vector on
    -- the renderer's sharedMaterial via Material.GetVector/GetFloat/SetVector; Roblox
    -- materials expose no arbitrary shader-vector API, so the scrolling-wave UV animation
    -- has no equivalent. Roblox renders water surfaces internally.
end

return WaterBasic