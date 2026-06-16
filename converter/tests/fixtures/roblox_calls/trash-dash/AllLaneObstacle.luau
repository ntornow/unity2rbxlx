local Obstacle = require(game:GetService("ReplicatedStorage"):FindFirstChild("Obstacle", true) or game:GetService("ServerStorage"):FindFirstChild("Obstacle", true))

local AllLaneObstacle = {}
AllLaneObstacle.__index = AllLaneObstacle

function AllLaneObstacle.new(config)
	-- Wire inheritance from Obstacle here (module scope forbids the setmetatable call).
	-- Idempotent: only link the class metatable once.
	if getmetatable(AllLaneObstacle) == nil then
		setmetatable(AllLaneObstacle, { __index = Obstacle })
	end
	local self = setmetatable({}, AllLaneObstacle)
	-- impactedSound (AudioClip) is inherited from Obstacle: an asset field injected
	-- on self as a rbxassetid string before Awake. Constructor reads scalar config only.
	return self
end

-- public override IEnumerator Spawn(TrackSegment segment, float t)
-- Coroutine: the spawner (TrackManager) drives this via self.host.startCoroutine.
function AllLaneObstacle:Spawn(segment, t)
	-- segment.GetPointAt(t, out position, out rotation):
	-- Luau GetPointAt returns (pos, rot) — pos is a Vector3, rot is a CFrame.
	local position, rotation = segment:GetPointAt(t)

	-- AsyncOperationHandle op = Addressables.InstantiateAsync(gameObject.name, position, rotation);
	-- yield return op;
	-- Name-based async load: no serialized prefab_id is injected for a dynamic name lookup,
	-- so resolve the template registered under this obstacle's name and clone it. Roblox
	-- cloning is synchronous, so there is no async handle to yield on.
	local templates = game:GetService("ReplicatedStorage"):FindFirstChild("Templates")
	local template = templates ~= nil and templates:FindFirstChild(self.gameObject.Name) or nil

	-- if (op.Result == null || !(op.Result is GameObject)) { Debug.LogWarning(...); yield break; }
	if template == nil then
		warn(string.format("Unable to load obstacle %s.", self.gameObject.Name))
		return
	end

	-- GameObject obj = op.Result as GameObject;
	local obj = template:Clone()

	-- Place at position with the sampled rotation (rotation is a CFrame; take its
	-- orientation and anchor it at position).
	obj:PivotTo(CFrame.new(position) * rotation.Rotation)

	-- obj.transform.SetParent(segment.objectRoot, true);
	obj.Parent = segment.objectRoot

	-- TODO : remove that hack related to #issue7
	-- Vector3 oldPos = obj.transform.position;
	local oldPos = obj:GetPivot().Position
	-- obj.transform.position += Vector3.back;  (Unity Vector3.back = (0, 0, -1))
	obj:PivotTo(CFrame.new(oldPos + Vector3.new(0, 0, -1)) * obj:GetPivot().Rotation)
	-- obj.transform.position = oldPos;
	obj:PivotTo(CFrame.new(oldPos) * obj:GetPivot().Rotation)
end

return AllLaneObstacle