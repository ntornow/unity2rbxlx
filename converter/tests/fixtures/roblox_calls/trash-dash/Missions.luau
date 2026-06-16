local AllLaneObstacle = require(game:GetService("ReplicatedStorage"):FindFirstChild("AllLaneObstacle", true) or game:GetService("ServerStorage"):FindFirstChild("AllLaneObstacle", true))

local STUDS_PER_METER = 3.571

local MissionBase = {}
MissionBase.__index = MissionBase

-- public enum MissionType -> table of named numeric values.
MissionBase.MissionType = {
	SINGLE_RUN = 0,
	PICKUP = 1,
	OBSTACLE_JUMP = 2,
	SLIDING = 3,
	MULTIPLIER = 4,
	MAX = 5,
}

function MissionBase.new()
	local self = setmetatable({}, MissionBase)
	self.progress = 0
	self.max = 0
	self.reward = 0
	return self
end

-- public bool isComplete { get { return (progress / max) >= 1.0f; } }
-- Computed getter with logic -> method (the codebase is method-based).
function MissionBase:isComplete()
	return (self.progress / self.max) >= 1.0
end

-- BinaryWriter/BinaryReader are host-provided serializer wrappers passed in by
-- the save system (PlayerData); call their methods faithfully.
function MissionBase:Serialize(w)
	w:Write(self.progress)
	w:Write(self.max)
	w:Write(self.reward)
end

function MissionBase:Deserialize(r)
	self.progress = r:ReadSingle()
	self.max = r:ReadSingle()
	self.reward = r:ReadInt32()
end

-- public virtual bool HaveProgressBar() { return true; }
function MissionBase:HaveProgressBar()
	return true
end

-- Abstract members: the base provides no body; subclasses below override.
function MissionBase:Created()
end

function MissionBase:GetMissionType()
end

function MissionBase:GetMissionDesc()
end

function MissionBase:RunStart(manager)
end

function MissionBase:Update(manager)
end

-- public class SingleRunMission : MissionBase
local SingleRunMission = setmetatable({}, { __index = MissionBase })
SingleRunMission.__index = SingleRunMission

function SingleRunMission.new()
	return setmetatable(MissionBase.new(), SingleRunMission)
end

function SingleRunMission:Created()
	local maxValues = { 500, 1000, 1500, 2000 }
	-- Random.Range(int, int) max is exclusive -> math.random(0, #-1).
	local choosenVal = math.random(0, #maxValues - 1)

	self.reward = choosenVal + 1
	self.max = maxValues[choosenVal + 1]
	self.progress = 0
end

function SingleRunMission:HaveProgressBar()
	return false
end

function SingleRunMission:GetMissionDesc()
	return "Run " .. math.floor(self.max) .. "m in a single run"
end

function SingleRunMission:GetMissionType()
	return MissionBase.MissionType.SINGLE_RUN
end

function SingleRunMission:RunStart(manager)
	self.progress = 0
end

function SingleRunMission:Update(manager)
	self.progress = manager.worldDistance
end

-- public class PickupMission : MissionBase
local PickupMission = setmetatable({}, { __index = MissionBase })
PickupMission.__index = PickupMission

function PickupMission.new()
	local self = setmetatable(MissionBase.new(), PickupMission)
	self.previousCoinAmount = 0
	return self
end

function PickupMission:Created()
	local maxValues = { 1000, 2000, 3000, 4000 }
	local choosen = math.random(0, #maxValues - 1)

	self.max = maxValues[choosen + 1]
	self.reward = choosen + 1
	self.progress = 0
end

function PickupMission:GetMissionDesc()
	return "Pickup " .. self.max .. " fishbones"
end

function PickupMission:GetMissionType()
	return MissionBase.MissionType.PICKUP
end

function PickupMission:RunStart(manager)
	self.previousCoinAmount = 0
end

function PickupMission:Update(manager)
	local coins = manager.characterController.coins - self.previousCoinAmount
	self.progress = self.progress + coins

	self.previousCoinAmount = manager.characterController.coins
end

-- public class BarrierJumpMission : MissionBase
local BarrierJumpMission = setmetatable({}, { __index = MissionBase })
BarrierJumpMission.__index = BarrierJumpMission

-- protected readonly Vector3 k_CharacterColliderSizeOffset = new Vector3(-0.3f, 2f, -0.3f)
-- Unity-metres literal -> studs.
local k_CharacterColliderSizeOffset = Vector3.new(-0.3, 2, -0.3) * STUDS_PER_METER

function BarrierJumpMission.new()
	local self = setmetatable(MissionBase.new(), BarrierJumpMission)
	self.m_Previous = nil
	return self
end

function BarrierJumpMission:Created()
	local maxValues = { 20, 50, 75, 100 }
	local choosen = math.random(0, #maxValues - 1)

	self.max = maxValues[choosen + 1]
	self.reward = choosen + 1
	self.progress = 0
end

function BarrierJumpMission:GetMissionDesc()
	return "Jump over " .. math.floor(self.max) .. " barriers"
end

function BarrierJumpMission:GetMissionType()
	return MissionBase.MissionType.OBSTACLE_JUMP
end

function BarrierJumpMission:RunStart(manager)
	self.m_Previous = nil
end

function BarrierJumpMission:Update(manager)
	if manager.characterController.isJumping then
		-- characterCollider.collider.size is the converted Roblox Part.Size (already
		-- in studs); the offset literal above was scaled metres->studs to match.
		local boxSize = manager.characterController.characterCollider.collider.size + k_CharacterColliderSizeOffset
		local charPos = manager.characterController.gameObject:GetPivot().Position
		local boxCenter = charPos - Vector3.new(0, 1, 0) * boxSize.Y * 0.5

		-- Physics.OverlapBoxNonAlloc(center, halfExtents, results): the second arg
		-- is half-extents, so the full box is boxSize. GetPartBoundsInBox takes the
		-- full size.
		local params = OverlapParams.new()
		local hits = workspace:GetPartBoundsInBox(CFrame.new(boxCenter), boxSize, params)

		for _, hit in hits do
			local obs = manager.host.getComponent(hit, "Obstacle")

			if obs ~= nil and getmetatable(obs) == AllLaneObstacle then
				if obs ~= self.m_Previous then
					self.progress = self.progress + 1
				end

				self.m_Previous = obs
			end
		end
	end
end

-- public class SlidingMission : MissionBase
local SlidingMission = setmetatable({}, { __index = MissionBase })
SlidingMission.__index = SlidingMission

function SlidingMission.new()
	local self = setmetatable(MissionBase.new(), SlidingMission)
	self.m_PreviousWorldDist = 0
	return self
end

function SlidingMission:Created()
	local maxValues = { 20, 30, 75, 150 }
	local choosen = math.random(0, #maxValues - 1)

	self.reward = choosen + 1
	self.max = maxValues[choosen + 1]
	self.progress = 0
end

function SlidingMission:GetMissionDesc()
	return "Slide for " .. math.floor(self.max) .. "m"
end

function SlidingMission:GetMissionType()
	return MissionBase.MissionType.SLIDING
end

function SlidingMission:RunStart(manager)
	self.m_PreviousWorldDist = manager.worldDistance
end

function SlidingMission:Update(manager)
	if manager.characterController.isSliding then
		local dist = manager.worldDistance - self.m_PreviousWorldDist
		self.progress = self.progress + dist
	end

	self.m_PreviousWorldDist = manager.worldDistance
end

-- public class MultiplierMission : MissionBase
local MultiplierMission = setmetatable({}, { __index = MissionBase })
MultiplierMission.__index = MultiplierMission

function MultiplierMission.new()
	return setmetatable(MissionBase.new(), MultiplierMission)
end

function MultiplierMission:HaveProgressBar()
	return false
end

function MultiplierMission:Created()
	local maxValue = { 3, 5, 8, 10 }
	local choosen = math.random(0, #maxValue - 1)

	self.max = maxValue[choosen + 1]
	self.reward = choosen + 1

	self.progress = 0
end

function MultiplierMission:GetMissionDesc()
	return "Reach a x" .. math.floor(self.max) .. " multiplier"
end

function MultiplierMission:GetMissionType()
	return MissionBase.MissionType.MULTIPLIER
end

function MultiplierMission:RunStart(manager)
	self.progress = 0
end

function MultiplierMission:Update(manager)
	if manager.multiplier > self.progress then
		self.progress = manager.multiplier
	end
end

-- static public MissionBase GetNewMissionFromType(MissionType type)
function MissionBase.GetNewMissionFromType(missionType)
	local MT = MissionBase.MissionType

	if missionType == MT.SINGLE_RUN then
		return SingleRunMission.new()
	elseif missionType == MT.PICKUP then
		return PickupMission.new()
	elseif missionType == MT.OBSTACLE_JUMP then
		return BarrierJumpMission.new()
	elseif missionType == MT.SLIDING then
		return SlidingMission.new()
	elseif missionType == MT.MULTIPLIER then
		return MultiplierMission.new()
	end

	return nil
end

return MissionBase