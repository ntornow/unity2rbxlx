local TrackManager = require(game:GetService("ReplicatedStorage"):FindFirstChild("TrackManager", true) or game:GetService("ServerStorage"):FindFirstChild("TrackManager", true))

local STUDS_PER_METER = 3.571

-- Animator parameter / state hashes (Animator.StringToHash) — mirrored as attribute names.
local s_DeadHash = "Dead"
local s_RunStartHash = "runStart"
local s_MovingHash = "Moving"
local s_JumpingHash = "Jumping"
local s_JumpingSpeedHash = "JumpSpeed"
local s_SlidingHash = "Sliding"

local k_StartingLane = 1
local k_GroundingSpeed = 80
local k_ShadowRaycastDistance = 100
local k_ShadowGroundOffset = 0.01
local k_TrackSpeedToJumpAnimSpeedRatio = 0.6
local k_TrackSpeedToSlideAnimSpeedRatio = 0.9

-- Mathf.MoveTowards for a scalar.
local function moveTowardsScalar(current, target, maxDelta)
	local diff = target - current
	if math.abs(diff) <= maxDelta then
		return target
	end
	return current + (diff > 0 and maxDelta or -maxDelta)
end

-- Vector3.MoveTowards(current, target, maxDelta).
local function moveTowardsVec(current, target, maxDelta)
	local diff = target - current
	local dist = diff.Magnitude
	if dist <= maxDelta or dist == 0 then
		return target
	end
	return current + diff / dist * maxDelta
end

local CharacterInputController = {}
CharacterInputController.__index = CharacterInputController

function CharacterInputController.new(config)
	local self = setmetatable({}, CharacterInputController)
	-- Scalar config only; defer host/peer/instance access to Awake/Init.
	self.laneChangeSpeed = config.laneChangeSpeed or 1.0
	self.maxLife = config.maxLife or 3
	self.jumpLength = config.jumpLength or 2.0
	self.jumpHeight = config.jumpHeight or 1.2
	self.slideLength = config.slideLength or 2.0

	-- Cross-script readable state (siblings read these as plain fields).
	self.coins = 0
	self.premium = 0
	self.currentLife = 0
	self.consumables = {}

	self.inventory = nil

	self.currentTutorialLevel = 0
	self.tutorialWaitingForValidation = false

	self.m_ObstacleLayer = 0
	self.m_IsInvincible = false
	self.m_IsRunning = false
	self.m_JumpStart = 0
	self.m_Jumping = false
	self.m_Sliding = false
	self.m_SlideStart = 0
	self.m_CurrentLane = k_StartingLane
	self.m_TargetPosition = Vector3.new(0, 0, 0)
	self.m_ColliderLocalPos = Vector3.new(0, 0, 0)
	self.m_Audio = nil
	return self
end

function CharacterInputController:Awake()
	self.premium = 0
	self.currentLife = 0
	self.m_Sliding = false
	self.m_SlideStart = 0.0
	self.m_IsRunning = false
end

-- Cheating functions, use for testing
function CharacterInputController:CheatInvincible(invincible)
	self.m_IsInvincible = invincible
end

function CharacterInputController:IsCheatInvincible()
	return self.m_IsInvincible
end

function CharacterInputController:isJumping()
	return self.m_Jumping
end

function CharacterInputController:isSliding()
	return self.m_Sliding
end

function CharacterInputController:Init()
	-- k_StartingPosition = Vector3.forward * 2; Unity +Z -> Roblox -Z.
	self.gameObject:PivotTo(CFrame.new(0, 0, -2 * STUDS_PER_METER))
	self.m_TargetPosition = Vector3.new(0, 0, 0)

	self.m_CurrentLane = k_StartingLane
	self.m_ColliderLocalPos = Vector3.new(0, 0, 0)
	self.characterCollider.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.new(self.m_ColliderLocalPos))

	self.currentLife = self.maxLife

	self.m_Audio = self:GetComponent("AudioSource")

	-- 1 << LayerMask.NameToLayer("Obstacle"): Unity layer mask has no Roblox raycast equivalent.
	self.m_ObstacleLayer = 0
end

-- Called at the beginning of a run or rerun
function CharacterInputController:Begin()
	self.m_IsRunning = false
	self.character.gameObject:SetAttribute(s_DeadHash, false) -- animator.SetBool(Dead, false)

	self.characterCollider:Init()

	table.clear(self.consumables)
end

function CharacterInputController:End()
	self:CleanConsumable()
end

function CharacterInputController:CleanConsumable()
	for i = 1, #self.consumables do
		self.consumables[i]:Ended(self)
		self.host.destroy(self.consumables[i].gameObject) -- Addressables.ReleaseInstance
	end

	table.clear(self.consumables)
end

function CharacterInputController:StartRunning()
	self:StartMoving()
	if self.character.animator then
		self.character.gameObject:SetAttribute(s_RunStartHash, true) -- animator.Play(runStart)
		self.character.gameObject:SetAttribute(s_MovingHash, true)
	end
end

function CharacterInputController:StartMoving()
	self.m_IsRunning = true
end

function CharacterInputController:StopMoving()
	self.m_IsRunning = false
	self.trackManager:StopMove()
	if self.character.animator then
		self.character.gameObject:SetAttribute(s_MovingHash, false)
	end
end

function CharacterInputController:TutorialMoveCheck(tutorialLevel)
	self.tutorialWaitingForValidation = self.currentTutorialLevel ~= tutorialLevel

	return (not TrackManager.instance.isTutorial) or self.currentTutorialLevel >= tutorialLevel
end

function CharacterInputController:Update(dt)
	-- Keyboard input (UNITY_EDITOR || UNITY_STANDALONE branch). Touch/swipe branch omitted on PC target.
	local UserInputService = game:GetService("UserInputService")

	if UserInputService:IsKeyDown(Enum.KeyCode.Left) and self:TutorialMoveCheck(0) then
		self:ChangeLane(-1)
	elseif UserInputService:IsKeyDown(Enum.KeyCode.Right) and self:TutorialMoveCheck(0) then
		self:ChangeLane(1)
	elseif UserInputService:IsKeyDown(Enum.KeyCode.Up) and self:TutorialMoveCheck(1) then
		self:Jump()
	elseif UserInputService:IsKeyDown(Enum.KeyCode.Down) and self:TutorialMoveCheck(2) then
		if not self.m_Sliding then
			self:Slide()
		end
	end

	local vtX = self.m_TargetPosition.X
	local vtY = self.m_TargetPosition.Y
	local vtZ = self.m_TargetPosition.Z

	if self.m_Sliding then
		-- Slide length is fixed (slightly modified by speed), slide time isn't.
		local correctSlideLength = self.slideLength * (1.0 + self.trackManager.speedRatio)
		local ratio = (self.trackManager.worldDistance - self.m_SlideStart) / correctSlideLength
		if ratio >= 1.0 then
			self:StopSliding()
		end
	end

	if self.m_Jumping then
		if self.trackManager.isMoving then
			-- Fixed jump LENGTH not fixed jump TIME, slightly modified by speed.
			local correctJumpLength = self.jumpLength * (1.0 + self.trackManager.speedRatio)
			local ratio = (self.trackManager.worldDistance - self.m_JumpStart) / correctJumpLength
			if ratio >= 1.0 then
				self.m_Jumping = false
				self.character.gameObject:SetAttribute(s_JumpingHash, false)
			else
				vtY = math.sin(ratio * math.pi) * (self.jumpHeight * STUDS_PER_METER)
			end
		else
			-- Unity guards this with !AudioListener.pause (no Roblox equivalent); assume not paused.
			vtY = moveTowardsScalar(vtY, 0, k_GroundingSpeed * STUDS_PER_METER * dt)
			if math.abs(vtY) < 0.0001 then -- Mathf.Approximately(vtY, 0)
				self.character.gameObject:SetAttribute(s_JumpingHash, false)
				self.m_Jumping = false
			end
		end
	end

	local verticalTargetPosition = Vector3.new(vtX, vtY, vtZ)
	self.m_ColliderLocalPos = moveTowardsVec(self.m_ColliderLocalPos, verticalTargetPosition, self.laneChangeSpeed * STUDS_PER_METER * dt)
	self.characterCollider.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.new(self.m_ColliderLocalPos))

	-- Put blob shadow under the character.
	local colliderPos = self.characterCollider.gameObject:GetPivot().Position
	local origin = colliderPos + Vector3.new(0, 1 * STUDS_PER_METER, 0)
	local params = RaycastParams.new()
	local hit = workspace:Raycast(origin, Vector3.new(0, -1, 0) * (k_ShadowRaycastDistance * STUDS_PER_METER), params)
	if hit then
		self.blobShadow:PivotTo(CFrame.new(hit.Position + Vector3.new(0, k_ShadowGroundOffset * STUDS_PER_METER, 0)))
	else
		self.blobShadow:PivotTo(CFrame.new(colliderPos.X, k_ShadowGroundOffset * STUDS_PER_METER, colliderPos.Z))
	end
end

function CharacterInputController:Jump()
	if not self.m_IsRunning then
		return
	end

	if not self.m_Jumping then
		if self.m_Sliding then
			self:StopSliding()
		end

		local correctJumpLength = self.jumpLength * (1.0 + self.trackManager.speedRatio)
		self.m_JumpStart = self.trackManager.worldDistance
		local animSpeed = k_TrackSpeedToJumpAnimSpeedRatio * (self.trackManager.speed / correctJumpLength)

		self.character.gameObject:SetAttribute(s_JumpingSpeedHash, animSpeed)
		self.character.gameObject:SetAttribute(s_JumpingHash, true)
		if self.m_Audio and self.character.jumpSound then -- PlayOneShot(character.jumpSound)
			self.m_Audio.SoundId = self.character.jumpSound
			self.m_Audio:Play()
		end
		self.m_Jumping = true
	end
end

function CharacterInputController:StopJumping()
	if self.m_Jumping then
		self.character.gameObject:SetAttribute(s_JumpingHash, false)
		self.m_Jumping = false
	end
end

function CharacterInputController:Slide()
	if not self.m_IsRunning then
		return
	end

	if not self.m_Sliding then
		if self.m_Jumping then
			self:StopJumping()
		end

		local correctSlideLength = self.slideLength * (1.0 + self.trackManager.speedRatio)
		self.m_SlideStart = self.trackManager.worldDistance
		local animSpeed = k_TrackSpeedToJumpAnimSpeedRatio * (self.trackManager.speed / correctSlideLength)

		self.character.gameObject:SetAttribute(s_JumpingSpeedHash, animSpeed)
		self.character.gameObject:SetAttribute(s_SlidingHash, true)
		if self.m_Audio and self.slideSound then -- PlayOneShot(slideSound)
			self.m_Audio.SoundId = self.slideSound
			self.m_Audio:Play()
		end
		self.m_Sliding = true

		self.characterCollider:Slide(true)
	end
end

function CharacterInputController:StopSliding()
	if self.m_Sliding then
		self.character.gameObject:SetAttribute(s_SlidingHash, false)
		self.m_Sliding = false

		self.characterCollider:Slide(false)
	end
end

function CharacterInputController:ChangeLane(direction)
	if not self.m_IsRunning then
		return
	end

	local targetLane = self.m_CurrentLane + direction

	if targetLane < 0 or targetLane > 2 then
		-- Ignore, we are on the borders.
		return
	end

	self.m_CurrentLane = targetLane
	self.m_TargetPosition = Vector3.new((self.m_CurrentLane - 1) * self.trackManager.laneOffset * STUDS_PER_METER, 0, 0)
end

function CharacterInputController:UseInventory()
	if self.inventory ~= nil and self.inventory:CanBeUsed(self) then
		self:UseConsumable(self.inventory)
		self.inventory = nil
	end
end

function CharacterInputController:UseConsumable(c)
	local _audio = self.characterCollider:getAudio()
	if _audio and self.powerUpUseSound then -- characterCollider.audio.PlayOneShot(powerUpUseSound)
		_audio.SoundId = self.powerUpUseSound
		_audio:Play()
	end

	for i = 1, #self.consumables do
		if getmetatable(self.consumables[i]) == getmetatable(c) then -- same GetType()
			-- If we already have an active consumable of that type, we just reset the time
			self.consumables[i]:ResetTime()
			self.host.destroy(c.gameObject) -- Addressables.ReleaseInstance
			return
		end
	end

	-- If we didn't have one, activate that one
	c.gameObject.Parent = self.gameObject -- c.transform.SetParent(transform, false)
	self.host.setActive(c.gameObject, false)

	table.insert(self.consumables, c)
	self.host.startCoroutine(self, function()
		c:Started(self)
	end)
end

return CharacterInputController