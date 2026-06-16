local ShopList = {}
ShopList.__index = ShopList

function ShopList.new(config)
    local self = setmetatable({}, ShopList)
    -- prefabItem / listRoot are serialized references injected on self by the host
    -- before Awake; do NOT read them here.
    self.m_RefreshCallback = nil
    return self
end

function ShopList:Open()
    self:Populate()
    self.host.setActive(self.gameObject, true)
end

function ShopList:Close()
    self.host.setActive(self.gameObject, false)
    self.m_RefreshCallback = nil
end

function ShopList:Refresh()
    self.m_RefreshCallback()
end

function ShopList:Populate()
    -- Abstract in C#; subclasses (ShopAccessoriesList, ShopCharacterList, ...) override.
end

return ShopList