-- NavAgent: NavMesh pathfinding emulation using Roblox PathfindingService.
--
-- Wraps PathfindingService to provide a Unity-NavMeshAgent-like API.
--
-- Usage:
--   local NavAgent = require(path.to.NavAgent)
--   local agent = NavAgent.new(humanoid, { speed = 16, radius = 2, height = 5 })
--   agent:SetDestination(Vector3.new(10, 0, 10))
--   -- Agent will automatically walk waypoint-by-waypoint to the destination.

local PathfindingService = game:GetService("PathfindingService")
local RunService = game:GetService("RunService")

local NavAgent = {}
NavAgent.__index = NavAgent

--- Create a new NavAgent.
--- @param humanoid Humanoid -- The humanoid to control.
--- @param config table? -- Optional configuration overrides.
--- @return NavAgent
function NavAgent.new(humanoid, config)
	local self = setmetatable({}, NavAgent)

	self.humanoid = humanoid
	self.rootPart = humanoid.RootPart
	self.speed = config and config.speed or 16
	self.stoppingDistance = config and config.stoppingDistance or 0.5
	self.angularSpeed = config and config.angularSpeed or 120
	self.destination = nil
	self.path = nil
	self.waypoints = nil
	self.currentWaypointIndex = 0
	self.isStopped = true
	self.remainingDistance = 0
	self.hasPath = false

	self.agentParams = {
		AgentRadius = config and config.radius or 2,
		AgentHeight = config and config.height or 5,
		AgentCanJump = config and config.canJump or true,
		AgentCanClimb = config and config.canClimb or false,
	}

	humanoid.WalkSpeed = self.speed

	return self
end

--- Compute a path to the target position and begin walking.
--- @param target Vector3 -- World-space destination.
function NavAgent:SetDestination(target)
	self.destination = target
	self.isStopped = false

	self.path = PathfindingService:CreatePath(self.agentParams)

	local success, err = pcall(function()
		self.path:ComputeAsync(self.rootPart.Position, target)
	end)

	if success and self.path.Status == Enum.PathStatus.Success then
		self.waypoints = self.path:GetWaypoints()
		-- Skip the first waypoint (agent's current position)
		self.currentWaypointIndex = 2
		self.hasPath = true
		self:_moveToNextWaypoint()
	else
		self.hasPath = false
		self.isStopped = true
		warn("[NavAgent] Path computation failed:", err)
	end
end

--- Walk to the next waypoint in the computed path.
function NavAgent:_moveToNextWaypoint()
	if not self.waypoints or self.currentWaypointIndex > #self.waypoints then
		self.isStopped = true
		self.hasPath = false
		return
	end

	local wp = self.waypoints[self.currentWaypointIndex]

	-- Jump if the waypoint requires it
	if wp.Action == Enum.PathWaypointAction.Jump then
		self.humanoid.Jump = true
	end

	self.humanoid:MoveTo(wp.Position)

	self.humanoid.MoveToFinished:Connect(function(reached)
		if reached and not self.isStopped then
			self.currentWaypointIndex = self.currentWaypointIndex + 1
			self:_moveToNextWaypoint()
		end
	end)
end

--- Stop the agent immediately at its current position.
function NavAgent:Stop()
	self.isStopped = true
	self.humanoid:MoveTo(self.rootPart.Position)
end

--- Calculate the remaining path distance from the agent to the destination.
--- @return number -- Distance in studs.
function NavAgent:GetRemainingDistance()
	if not self.waypoints or not self.rootPart then
		return 0
	end

	local dist = 0
	local prev = self.rootPart.Position

	for i = self.currentWaypointIndex, #self.waypoints do
		dist = dist + (self.waypoints[i].Position - prev).Magnitude
		prev = self.waypoints[i].Position
	end

	return dist
end

return NavAgent
