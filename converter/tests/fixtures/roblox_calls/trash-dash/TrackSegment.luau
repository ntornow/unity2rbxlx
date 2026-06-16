local Coin = require(game:GetService("ReplicatedStorage"):FindFirstChild("Coin", true) or game:GetService("ServerStorage"):FindFirstChild("Coin", true))

local TrackSegment = {}
TrackSegment.__index = TrackSegment

function TrackSegment.new(config)
	local self = setmetatable({}, TrackSegment)
	-- Reference fields are NOT read here: pathParent + possibleObstacles (AssetReference[])
	-- are injected before Awake; manager is set by TrackManager; objectRoot and
	-- collectibleTransform are created in OnEnable. Constructor reads scalar config only.
	self.obstaclePositions = config.obstaclePositions or {}
	self.manager = nil
	self.objectRoot = nil
	self.collectibleTransform = nil
	-- worldLength is a read-only property over m_WorldLength; mirror it as a plain
	-- field so cross-script readers (TrackManager) can read segment.worldLength.
	self.m_WorldLength = 0
	self.worldLength = 0
	return self
end

function TrackSegment:OnEnable()
	self:UpdateWorldLength()

	-- new GameObject("ObjectRoot"); obj.transform.SetParent(transform).
	local objRoot = Instance.new("Model")
	objRoot.Name = "ObjectRoot"
	objRoot.Parent = self.gameObject
	self.objectRoot = objRoot

	-- new GameObject("Collectibles"); obj.transform.SetParent(objectRoot).
	local collectibles = Instance.new("Model")
	collectibles.Name = "Collectibles"
	collectibles.Parent = objRoot
	self.collectibleTransform = collectibles
end

-- Same as GetPointAt but using an interpolation parameter in world units instead of 0 to 1.
function TrackSegment:GetPointAtInWorldUnit(wt)
	local t = wt / self.m_WorldLength
	return self:GetPointAt(t)
end

-- Interpolation parameter t is clamped between 0 and 1. Returns (pos, rot):
-- pos is a Vector3, rot is a CFrame (CFrame holds rotation in Roblox).
function TrackSegment:GetPointAt(t)
	local clampedT = math.clamp(t, 0, 1)
	local children = self.pathParent:GetChildren()
	local childCount = #children
	local scaledT = (childCount - 1) * clampedT
	local index = math.floor(scaledT)
	local segmentT = scaledT - index

	local orig = children[index + 1]
	local origPivot = orig:GetPivot()
	if index == childCount - 1 then
		return origPivot.Position, origPivot
	end

	local target = children[index + 2]
	local targetPivot = target:GetPivot()

	local pos = origPivot.Position:Lerp(targetPivot.Position, segmentT)
	local rot = origPivot:Lerp(targetPivot, segmentT)
	return pos, rot
end

function TrackSegment:UpdateWorldLength()
	self.m_WorldLength = 0

	local children = self.pathParent:GetChildren()
	for i = 1, #children - 1 do
		local orig = children[i]
		local endChild = children[i + 1]

		local vec = endChild:GetPivot().Position - orig:GetPivot().Position
		self.m_WorldLength = self.m_WorldLength + vec.Magnitude
	end

	self.worldLength = self.m_WorldLength
end

function TrackSegment:Cleanup()
	local children = self.collectibleTransform:GetChildren()
	while #children > 0 do
		local t = children[1]
		t.Parent = nil
		Coin.coinPool:Free(t)
		children = self.collectibleTransform:GetChildren()
	end

	-- Addressables.ReleaseInstance(gameObject).
	self.host.destroy(self.gameObject)
end

function TrackSegment:OnDrawGizmos()
	-- UNCONVERTED: editor-only gizmo drawing (UnityEditor Gizmos API has no Roblox runtime equivalent).
end

return TrackSegment