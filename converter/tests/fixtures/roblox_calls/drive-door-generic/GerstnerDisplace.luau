-- GerstnerDisplace: empty subclass of a Roblox-dead base (no Roblox equivalent) -- inert stub (host-instantiable, no-op).
local GerstnerDisplace = {}
GerstnerDisplace.__index = GerstnerDisplace

function GerstnerDisplace.new(config)
    return setmetatable({ config = config }, GerstnerDisplace)
end

function GerstnerDisplace:Awake()
end

return GerstnerDisplace
