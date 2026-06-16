local OpenURL = {}
OpenURL.__index = OpenURL

function OpenURL.new(config)
    local self = setmetatable({}, OpenURL)
    self.websiteAddress = config.websiteAddress or ""
    return self
end

function OpenURL:OpenURLOnClick()
    -- UNCONVERTED: Application.OpenURL has no Roblox equivalent — Roblox sandboxes scripts and cannot open an arbitrary external web URL from gameplay code.
end

return OpenURL