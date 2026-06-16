local LevelLoader = {}
LevelLoader.__index = LevelLoader

function LevelLoader.new(config)
    local self = setmetatable({}, LevelLoader)
    return self
end

function LevelLoader:LoadLevel(name)
    -- UNCONVERTED: SceneManager.LoadScene has no Roblox equivalent (Roblox has no runtime scene-swap API; places are loaded via TeleportService, not by scene name).
end

return LevelLoader