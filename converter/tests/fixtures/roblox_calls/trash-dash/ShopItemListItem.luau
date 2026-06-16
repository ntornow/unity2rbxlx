local ShopItemListItem = {}
ShopItemListItem.__index = ShopItemListItem

function ShopItemListItem.new(config)
    local self = setmetatable({}, ShopItemListItem)
    -- Pure constructor: no scalar config on this component.
    -- Serialized references (icon, nameText, pricetext, premiumText,
    -- buyButton, countText, buyButtonSprite, disabledButtonSprite) are
    -- injected on self by the host before Awake; do not read them here.
    return self
end

return ShopItemListItem