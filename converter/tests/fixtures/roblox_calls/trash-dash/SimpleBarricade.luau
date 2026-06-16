local TrackManager = require(game:GetService("ReplicatedStorage"):FindFirstChild("TrackManager", true) or game:GetService("ServerStorage"):FindFirstChild("TrackManager", true))
local Obstacle = require(game:GetService("ReplicatedStorage"):FindFirstChild("Obstacle", true) or game:GetService("ServerStorage"):FindFirstChild("Obstacle", true))

local STUDS_PER_METER = 3.571

-- protected const obstacle-count / lane-index bounds (side-effect-free locals).
local k_MinObstacleCount = 1
local k_MaxObstacleCount = 2
local k_LeftMostLaneIndex = -1
local k_RightMostLaneIndex = 1

-- class SimpleBarricade : Obstacle -- plain class table at module scope (a literal,
-- no calls). The base-class __index link to Obstacle is established lazily inside
-- new() so module scope stays side-effect-free per the contract.
local SimpleBarricade = {}
SimpleBarricade.__index = SimpleBarricade

function SimpleBarricade.new(config)
	-- Link SimpleBarricade -> Obstacle for inherited Setup()/Impacted() lookup.
	-- Idempotent: only set the class metatable once.
	if not getmetatable(SimpleBarricade) then
		setmetatable(SimpleBarricade, { __index = Obstacle })
	end
	local self = setmetatable({}, SimpleBarricade)
	-- No serialized scalars on this obstacle; constructor stays pure (no host access).
	return self
end

-- public override IEnumerator Spawn(TrackSegment segment, float t)
-- Driven via self.host.startCoroutine (TrackManager runs it inside its own coroutine).
function SimpleBarricade:Spawn(segment, t)
	-- The tutorial's very first barricade must be centered and alone, so the player
	-- can swipe safely in either direction to avoid it.
	local isTutorialFirst = TrackManager.instance.isTutorial
		and TrackManager.instance.firstObstacle
		and segment == segment.manager.currentSegment

	if isTutorialFirst then
		TrackManager.instance.firstObstacle = false
	end

	-- Random.Range(1, k_MaxObstacleCount + 1) int -> math.random(1, 2).
	local count = isTutorialFirst and 1 or math.random(k_MinObstacleCount, k_MaxObstacleCount)
	-- Random.Range(-1, k_RightMostLaneIndex + 1) int -> math.random(-1, 1).
	local startLane = isTutorialFirst and 0 or math.random(k_LeftMostLaneIndex, k_RightMostLaneIndex)

	-- segment.GetPointAt(t, out position, out rotation): GetPointAt returns (pos, rot).
	local position, rotation = segment:GetPointAt(t)

	for i = 0, count - 1 do
		local lane = startLane + i
		if lane > k_RightMostLaneIndex then
			lane = k_LeftMostLaneIndex
		end

		-- Addressables.InstantiateAsync(gameObject.name, position, rotation) + yield return op:
		-- spawn the same obstacle prefab by name (synchronous in the host model).
		local obj = self.host.instantiatePrefab(self.gameObject.Name, nil, rotation.Rotation + position)
		if obj == nil then
			-- op.Result == null || !(op.Result is GameObject) -> yield break.
			warn(string.format("Unable to load obstacle %s.", tostring(self.gameObject.Name)))
			return
		end

		-- obj.transform.position += obj.transform.right * lane * laneOffset (metres -> studs).
		obj:PivotTo(obj:GetPivot() + obj:GetPivot().RightVector * (lane * segment.manager.laneOffset * STUDS_PER_METER))

		-- obj.transform.SetParent(segment.objectRoot, true): reparent, keep world position.
		obj.Parent = segment.objectRoot

		-- TODO hack (#issue7): nudge then restore (net no-op).
		local oldPos = obj:GetPivot().Position
		obj:PivotTo(obj:GetPivot() + Vector3.new(0, 0, 1)) -- Vector3.back -> Roblox +Z
		obj:PivotTo(obj:GetPivot().Rotation + oldPos)
	end
end

return SimpleBarricade