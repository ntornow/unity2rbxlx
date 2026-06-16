local ConsumableType = {
	NONE = 0,
	COIN_MAG = 1,
	SCORE_MULTIPLAYER = 2,
	INVINCIBILITY = 3,
	EXTRALIFE = 4,
	MAX_COUNT = 5,
}

local Consumable = {}
Consumable.__index = Consumable
Consumable.ConsumableType = ConsumableType

function Consumable.new(config)
	local self = setmetatable({}, Consumable)
	-- Scalar config only; asset/prefab fields (icon, activatedSound,
	-- ActivatedParticleReference) are injected on self by the host before Awake.
	self.duration = config.duration or 0
	if config.canBeSpawned ~= nil then
		self.canBeSpawned = config.canBeSpawned
	else
		self.canBeSpawned = true
	end

	self.m_Active = true
	self.m_SinceStart = 0
	self.m_ParticleSpawned = nil
	self.m_LastTick = nil
	return self
end

-- active { get { return m_Active; } }
function Consumable:active()
	return self.m_Active
end

-- timeActive { get { return m_SinceStart; } }
function Consumable:timeActive()
	return self.m_SinceStart
end

-- Abstract: each derived consumable supplies its own data.
function Consumable:GetConsumableType()
	error("Consumable:GetConsumableType is abstract and must be overridden")
end

function Consumable:GetConsumableName()
	error("Consumable:GetConsumableName is abstract and must be overridden")
end

function Consumable:GetPrice()
	error("Consumable:GetPrice is abstract and must be overridden")
end

function Consumable:GetPremiumCost()
	error("Consumable:GetPremiumCost is abstract and must be overridden")
end

function Consumable:ResetTime()
	self.m_SinceStart = 0
end

-- override this to test whether a consumable is usable (e.g. ExtraLife at full health)
function Consumable:CanBeUsed(c)
	return true
end

-- IEnumerator Started(c) — driven via self.host.startCoroutine by the caller.
function Consumable:Started(c)
	self.m_SinceStart = 0

	if self.activatedSound ~= nil then
		c.powerupSource.SoundId = self.activatedSound
		c.powerupSource:Play()
	end

	if self.ActivatedParticleReference ~= nil then
		-- Addressables InstantiateAsync -> host prefab spawn (synchronous; no yield).
		-- op.Result is the spawned gameObject; m_ParticleSpawned holds it directly.
		local op = self.host.instantiatePrefab(self.ActivatedParticleReference, c.characterCollider.gameObject, c.characterCollider.gameObject:GetPivot())
		self.m_ParticleSpawned = op

		-- ParticleSystem.main.loop / .main.duration have no Roblox equivalent;
		-- approximated via attributes the prefab can carry.
		local loop = op:GetAttribute("ParticleLoop") or false
		if not loop then
			local mainDuration = op:GetAttribute("ParticleDuration") or 0
			self.host.startCoroutine(self, function()
				self:TimedRelease(op, mainDuration)
			end)
		end
		-- transform.SetParent + localPosition handled by instantiatePrefab's parent + pivot.
	end
end

-- IEnumerator TimedRelease(obj, time) — driven via startCoroutine.
function Consumable:TimedRelease(obj, time)
	task.wait(time)
	self.host.destroy(obj) -- Addressables.ReleaseInstance
end

function Consumable:Tick(c)
	-- By default do nothing, override to do per frame manipulation.
	-- Time.deltaTime has no host-provided value here; approximate via os.clock delta.
	local now = os.clock()
	local dt = self.m_LastTick and (now - self.m_LastTick) or 0
	self.m_LastTick = now

	self.m_SinceStart = self.m_SinceStart + dt
	if self.m_SinceStart >= self.duration then
		self.m_Active = false
		return
	end
end

function Consumable:Ended(c)
	if self.m_ParticleSpawned ~= nil then
		local loop = self.m_ParticleSpawned:GetAttribute("ParticleLoop") or false
		if loop then
			self.host.destroy(self.m_ParticleSpawned) -- Addressables.ReleaseInstance
		end
	end

	if self.activatedSound ~= nil and c.powerupSource.SoundId == self.activatedSound then
		c.powerupSource:Stop() -- if this is the one using the audio source, stop it
	end

	for i = 1, #c.consumables do
		if c.consumables[i]:active() and c.consumables[i].activatedSound ~= nil then
			-- if there is still an active consumable that has a sound, this is the one playing now
			c.powerupSource.SoundId = c.consumables[i].activatedSound
			c.powerupSource:Play()
		end
	end
end

return Consumable