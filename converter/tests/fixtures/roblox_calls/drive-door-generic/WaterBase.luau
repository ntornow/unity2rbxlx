-- WaterBase: Unity visual/rendering effect (no Roblox equivalent) -- inert stub (host-instantiable, no-op).
local WaterBase = {}
WaterBase.__index = WaterBase

function WaterBase.new(config)
    return setmetatable({ config = config }, WaterBase)
end

function WaterBase:Awake()
end

return WaterBase
