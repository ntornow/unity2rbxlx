local ConsumableIcon = {}
ConsumableIcon.__index = ConsumableIcon

function ConsumableIcon.new(config)
    local self = setmetatable({}, ConsumableIcon)
    return self
end

return ConsumableIcon