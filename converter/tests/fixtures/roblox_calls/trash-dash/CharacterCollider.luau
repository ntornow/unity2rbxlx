local Coin = require(game:GetService("ReplicatedStorage"):FindFirstChild("Coin", true) or game:GetService("ServerStorage"):FindFirstChild("Coin", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local TrackManager = require(game:GetService("ReplicatedStorage"):FindFirstChild("TrackManager", true) or game:GetService("ServerStorage"):FindFirstChild("TrackManager", true))

local STUDS_PER_METER = 3.571

-- Vector3.Scale component-wise multiply.
local function scaleVector(a, b)
	return Vector3.new(a.X * b.X, a.Y * b.Y, a.Z * b.Z)
end

-- Vector3.MoveTowards(current, target, maxDelta).
local function moveTowards(current, target, maxDelta)
	local diff = target - current
	local dist = diff.Magnitude
	if dist <= maxDelta or dist == 0 then
		return target
	end
	return current + diff / dist * maxDelta
end

-- static int s_HitHash = Animator.StringToHash("Hit")
local s_HitHash = "Hit"
-- Shader uniform "_BlinkingValue" — mirrored as an attribute (Roblox has no global shader uniforms).
local s_BlinkingValueName = "BlinkingValue"

local k_MagnetSpeed = 10
local k_CoinsLayerIndex = 8
local k_ObstacleLayerIndex = 9
local k_PowerupLayerIndex = 10
local k_DefaultInvinsibleTime = 2

local CharacterCollider = {}
CharacterCollider.__index = CharacterCollider

function CharacterCollider.new(config)
	local self = setmetatable({}, CharacterCollider)
	-- Read scalar config only; defer host/peer/instance access to Awake/Start.
	self.magnetCoins = {}
	self.m_TutorialHitObstacle = false
	self.m_Invincible = false
	self.m_StartingColliderHeight = 0
	self.m_DeathData = {
		character = "",
		obstacleType = "",
		themeUsed = "",
		coins = 0,
		premium = 0,
		score = 0,
		worldDistance = 0,
	}
	return self
end

function CharacterCollider:Awake()
	-- OnTriggerEnter(c)
	self.host:connectGameObjectSignal(self.gameObject, "Touched", function(c)
		-- Unity layers have no Roblox equivalent; the converter mirrors layer as the "UnityLayer" attribute.
		local layer = c:GetAttribute("UnityLayer")

		if layer == k_CoinsLayerIndex then
			local idx = table.find(self.magnetCoins, c)
			if idx then
				table.remove(self.magnetCoins, idx)
			end

			if c:GetAttribute("isPremium") then
				self.host.destroy(c)
				PlayerData.instance.premium = PlayerData.instance.premium + 1
				self.controller.premium = self.controller.premium + 1
				if self.m_Audio then
					self.m_Audio.SoundId = self.premiumSound
					self.m_Audio:Play()
				end
			else
				if Coin.coinPool then
					Coin.coinPool:Free(c)
				end
				PlayerData.instance.coins = PlayerData.instance.coins + 1
				self.controller.coins = self.controller.coins + 1
				if self.m_Audio then
					self.m_Audio.SoundId = self.coinSound
					self.m_Audio:Play()
				end
			end
		elseif layer == k_ObstacleLayerIndex then
			if self.m_Invincible or self.controller:IsCheatInvincible() then
				return
			end

			self.controller:StopMoving()

			c.CanTouch = false

			-- Cross-object converted component (Obstacle) — signalled to play its impact via the attribute mirror.
			local obstacleType = c:GetAttribute("ObstacleType")
			if obstacleType ~= nil then
				c:SetAttribute("Impacted", true)
			else
				self.host.destroy(c)
			end

			if TrackManager.instance.isTutorial then
				self.m_TutorialHitObstacle = true
			else
				self.controller.currentLife = self.controller.currentLife - 1
			end

			self.controller.character.gameObject:SetAttribute(s_HitHash, true)

			if self.controller.currentLife > 0 then
				if self.m_Audio then
					self.m_Audio.SoundId = self.controller.character.hitSound
					self.m_Audio:Play()
				end
				self:SetInvincible()
			else
				-- The collision killed the player, record all data to analytics.
				if self.m_Audio then
					self.m_Audio.SoundId = self.controller.character.deathSound
					self.m_Audio:Play()
				end

				self.m_DeathData.character = self.controller.character.characterName
				self.m_DeathData.themeUsed = self.controller.trackManager.currentTheme.themeName
				self.m_DeathData.obstacleType = obstacleType or c.Name
				self.m_DeathData.coins = self.controller.coins
				self.m_DeathData.premium = self.controller.premium
				self.m_DeathData.score = self.controller.trackManager.score
				self.m_DeathData.worldDistance = self.controller.trackManager.worldDistance
			end
		elseif layer == k_PowerupLayerIndex then
			-- Consumable lives on the touched object; controller resolves & uses it.
			self.controller:UseConsumable(c)
		end
	end)
end

function CharacterCollider:Start()
	self.m_Collider = self:GetComponent("BoxCollider")
	self.m_Audio = self:GetComponent("AudioSource")
	if self.m_Collider then
		self.m_StartingColliderHeight = self.m_Collider.Size.Y
	end
end

function CharacterCollider:Init()
	if self.koParticle then
		self.host.setActive(self.koParticle, false)
	end
	self.m_Invincible = false
end

function CharacterCollider:Slide(sliding)
	if not self.m_Collider then
		return
	end

	local k_SlidingColliderScale = Vector3.new(1.0, 0.5, 1.0)
	local k_NotSlidingColliderScale = Vector3.new(1.0, 2.0, 1.0)

	if sliding then
		self.m_Collider.Size = scaleVector(self.m_Collider.Size, k_SlidingColliderScale)
		self.m_Collider.CFrame = self.m_Collider.CFrame - Vector3.new(0.0, self.m_Collider.Size.Y * 0.5, 0.0)
	else
		self.m_Collider.CFrame = self.m_Collider.CFrame + Vector3.new(0.0, self.m_Collider.Size.Y * 0.5, 0.0)
		self.m_Collider.Size = scaleVector(self.m_Collider.Size, k_NotSlidingColliderScale)
	end
end

function CharacterCollider:Update(dt)
	-- Every coin registered to magnetCoins (magnet powerup) is dragged toward the player.
	local playerPos = self.gameObject:GetPivot().Position
	for i = 1, #self.magnetCoins do
		local coin = self.magnetCoins[i]
		local newPos = moveTowards(coin:GetPivot().Position, playerPos, k_MagnetSpeed * STUDS_PER_METER * dt)
		coin:PivotTo(CFrame.new(newPos))
	end
end

function CharacterCollider:SetInvincibleExplicit(invincible)
	self.m_Invincible = invincible
end

function CharacterCollider:SetInvincible(timer)
	timer = timer or k_DefaultInvinsibleTime
	self.host.startCoroutine(self, function()
		self:InvincibleTimer(timer)
	end)
end

function CharacterCollider:InvincibleTimer(timer)
	self.m_Invincible = true

	local time = 0
	local currentBlink = 1.0
	local lastBlink = 0.0
	local blinkPeriod = 0.1

	while time < timer and self.m_Invincible do
		self.gameObject:SetAttribute(s_BlinkingValueName, currentBlink)

		-- Check every frame instead of waiting a full blink period, so a slow frame still blinks.
		task.wait()
		time = time + 0.016666667
		lastBlink = lastBlink + 0.016666667

		if blinkPeriod < lastBlink then
			lastBlink = 0
			currentBlink = 1.0 - currentBlink
		end
	end

	self.gameObject:SetAttribute(s_BlinkingValueName, 0.0)

	self.m_Invincible = false
end

function CharacterCollider:getDeathData()
	return self.m_DeathData
end

function CharacterCollider:getCollider()
	return self.m_Collider
end

function CharacterCollider:getAudio()
	return self.m_Audio
end

function CharacterCollider:getTutorialHitObstacle()
	return self.m_TutorialHitObstacle
end

function CharacterCollider:setTutorialHitObstacle(value)
	self.m_TutorialHitObstacle = value
end

return CharacterCollider