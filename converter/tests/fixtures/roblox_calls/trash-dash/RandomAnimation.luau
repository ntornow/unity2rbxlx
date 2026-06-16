-- Configuration (from serialized fields)
local parameter = ""
local count = 0

local RandomAnimation = {}

function RandomAnimation.setParameter(value)
	parameter = value
end

function RandomAnimation.setCount(value)
	count = value
end

-- OnStateEnter: records the animator integer parameter on the host.
-- Animator params drive no skeleton in Roblox — recorded as an attribute only.
function RandomAnimation.OnStateEnter(animator, stateInfo, layerIndex)
	if animator then
		animator:SetAttribute(parameter, math.random(0, count - 1))
	end
end

return RandomAnimation