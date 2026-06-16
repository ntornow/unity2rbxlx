local HighscoreUI = {}
HighscoreUI.__index = HighscoreUI

function HighscoreUI.new(config)
    local self = setmetatable({}, HighscoreUI)
    -- Serialized UI references (number, playerName, inputName, score) are
    -- injected on self by the host before Awake; nothing scalar to read here.
    return self
end

return HighscoreUI