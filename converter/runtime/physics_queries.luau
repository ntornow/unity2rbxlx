-- UnityBridge/PhysicsQueries
-- Maps Unity's Physics query API (Raycast, CheckSphere, OverlapSphere)
-- to Roblox equivalents. Complements physics_bridge.luau which handles
-- CharacterController emulation (Move/SimpleMove/IsGrounded).
--
-- Usage:
--   local Physics = require(ReplicatedStorage.UnityBridge.PhysicsQueries)
--   local hit = Physics.Raycast(origin, direction, maxDistance)
--   local overlaps = Physics.OverlapSphere(center, radius)

local Physics = {}

function Physics.Raycast(origin, direction, maxDistance)
	maxDistance = maxDistance or 1000
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	params.FilterDescendantsInstances = {}

	local result = workspace:Raycast(origin, direction * maxDistance, params)
	if result then
		return {
			hit = true,
			point = result.Position,
			normal = result.Normal,
			distance = result.Distance,
			collider = result.Instance,
		}
	end
	return { hit = false }
end

function Physics.CheckSphere(center, radius, filter)
	-- Check if any parts overlap a sphere at center with given radius
	local region = Region3.new(
		center - Vector3.new(radius, radius, radius),
		center + Vector3.new(radius, radius, radius)
	)

	local parts = workspace:FindPartsInRegion3(region, nil, 20)
	for _, part in ipairs(parts) do
		local dist = (part.Position - center).Magnitude
		if dist <= radius + part.Size.Magnitude * 0.5 then
			return true, part
		end
	end
	return false, nil
end

function Physics.OverlapSphere(center, radius)
	local results = {}
	local region = Region3.new(
		center - Vector3.new(radius, radius, radius),
		center + Vector3.new(radius, radius, radius)
	)

	local parts = workspace:FindPartsInRegion3(region, nil, 50)
	for _, part in ipairs(parts) do
		local dist = (part.Position - center).Magnitude
		if dist <= radius + part.Size.Magnitude * 0.5 then
			table.insert(results, part)
		end
	end
	return results
end

return Physics
