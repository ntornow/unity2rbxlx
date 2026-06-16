local GameState = require(game:GetService("ReplicatedStorage"):FindFirstChild("GameState", true) or game:GetService("ServerStorage"):FindFirstChild("GameState", true))

-- public class TutorialState : GameState {} -- empty subclass: inherits every
-- field and method from GameState verbatim. Set up metatable inheritance so
-- TutorialState instances fall back to GameState methods, and chain the
-- constructor through GameState.new.
local TutorialState = setmetatable({}, { __index = GameState })
TutorialState.__index = TutorialState

function TutorialState.new(config)
	local self = GameState.new(config)
	return setmetatable(self, TutorialState)
end

return TutorialState