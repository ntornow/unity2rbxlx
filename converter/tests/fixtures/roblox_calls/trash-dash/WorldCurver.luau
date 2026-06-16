-- WorldCurver: Unity visual/rendering effect (no Roblox equivalent) -- inert stub (host-instantiable, no-op).
local WorldCurver = {}
WorldCurver.__index = WorldCurver

function WorldCurver.new(config)
    return setmetatable({ config = config }, WorldCurver)
end

function WorldCurver:Awake()
end

return WorldCurver
