-- CharacterBridge: Unity CharacterController emulation on Roblox.
--
-- Maps Unity's CharacterController.Move / SimpleMove / isGrounded to Roblox's
-- Humanoid API with ground detection via raycasting.
--
-- Usage:
--   local CharacterBridge = require(path.to.CharacterBridge)
--   local bridge = CharacterBridge.new(character, { speed = 16, slopeLimit = 45 })
--   bridge:Move(Vector3.new(1, 0, 0))
--   print(bridge:IsGrounded())

local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")

local CharacterBridge = {}
CharacterBridge.__index = CharacterBridge

--- Create a new CharacterBridge for the given character model.
--- @param character Model -- The character model (must contain a Humanoid).
--- @param config table? -- Optional configuration overrides.
--- @return CharacterBridge
function CharacterBridge.new(character, config)
	local self = setmetatable({}, CharacterBridge)

	self.character = character
	self.humanoid = character:FindFirstChildOfClass("Humanoid")
	self.rootPart = character:FindFirstChild("HumanoidRootPart") or character.PrimaryPart

	-- Unity CharacterController properties
	self.slopeLimit = config and config.slopeLimit or 45
	self.stepOffset = config and config.stepOffset or 0.3
	self.skinWidth = config and config.skinWidth or 0.08
	self.gravity = config and config.gravity or -9.81

	-- Runtime state
	self.velocity = Vector3.zero
	self.isGrounded = false
	self.moveDirection = Vector3.zero

	-- Apply config to humanoid
	if self.humanoid then
		self.humanoid.WalkSpeed = config and config.speed or 16
		self.humanoid.JumpPower = config and config.jumpPower or 50
		self.humanoid.MaxSlopeAngle = self.slopeLimit
	end

	return self
end

--- Move the character in the given direction (world-space).
--- Equivalent to Unity's CharacterController.Move().
--- @param direction Vector3 -- Movement direction vector.
function CharacterBridge:Move(direction)
	self.moveDirection = direction
	if self.humanoid and self.rootPart then
		self.humanoid:Move(Vector3.new(direction.X, 0, direction.Z))
	end
end

--- Move the character at a given speed vector.
--- Equivalent to Unity's CharacterController.SimpleMove().
--- @param speed Vector3 -- Speed vector.
function CharacterBridge:SimpleMove(speed)
	if self.humanoid then
		self.humanoid:Move(speed)
	end
end

--- Check whether the character is currently touching the ground.
--- Uses a downward raycast from the root part.
--- @return boolean
function CharacterBridge:IsGrounded()
	if not self.rootPart then
		return false
	end

	local params = RaycastParams.new()
	params.FilterDescendantsInstances = { self.character }

	local result = workspace:Raycast(
		self.rootPart.Position,
		Vector3.new(0, -(self.rootPart.Size.Y / 2 + 0.5), 0),
		params
	)

	self.isGrounded = result ~= nil
	return self.isGrounded
end

--- Override the gravity value and update workspace.Gravity accordingly.
--- @param g number -- Gravity in Unity units (negative = downward).
function CharacterBridge:SetGravity(g)
	self.gravity = g
	-- Roblox workspace.Gravity is in studs/s^2 (positive = downward).
	-- Unity default gravity magnitude is 9.81; Roblox default is 196.2 (= 9.81 * 20).
	workspace.Gravity = math.abs(g) * 20
end

return CharacterBridge
