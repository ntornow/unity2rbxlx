local WaterTile = {}
WaterTile.__index = WaterTile

function WaterTile.new(config)
    local self = setmetatable({}, WaterTile)
    -- reflection / waterBase are peer-component references resolved at runtime
    -- in AcquireComponents; do NOT touch the host surface here (rule e).
    self.reflection = nil
    self.waterBase = nil
    return self
end

function WaterTile:Start()
    self:AcquireComponents()
end

function WaterTile:AcquireComponents()
    if not self.reflection then
        -- Unity walks transform.parent.GetComponent<PlanarReflection>() with a
        -- transform.GetComponent fallback. The host component registry is keyed
        -- per-GameObject (no ancestor-indexed lookup), so both branches collapse
        -- to the local peer-component resolution.
        self.reflection = self:GetComponent("PlanarReflection")
    end
    if not self.waterBase then
        self.waterBase = self:GetComponent("WaterBase")
    end
end

function WaterTile:OnWillRenderObject()
    -- UNCONVERTED: OnWillRenderObject is a per-render-frame callback the host does
    -- not dispatch (Roblox renders water/reflections internally, with no manual
    -- Camera.current / per-object render hook). The peer calls it would drive --
    -- PlanarReflection.WaterTileBeingRendered and WaterBase.WaterTileBeingRendered --
    -- are themselves render-path UNCONVERTED stubs with no Roblox equivalent.
end

return WaterTile