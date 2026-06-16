local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local ThemeDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ThemeDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ThemeDatabase", true))
local MusicPlayer = require(game:GetService("ReplicatedStorage"):FindFirstChild("MusicPlayer", true) or game:GetService("ServerStorage"):FindFirstChild("MusicPlayer", true))
local Coin = require(game:GetService("ReplicatedStorage"):FindFirstChild("Coin", true) or game:GetService("ServerStorage"):FindFirstChild("Coin", true))
local Pooler = require(game:GetService("ReplicatedStorage"):FindFirstChild("Pooler", true) or game:GetService("ServerStorage"):FindFirstChild("Pooler", true))

local STUDS_PER_METER = 3.571

-- static int s_StartHash = Animator.StringToHash("Start") -> attribute name.
local s_StartHash = "Start"

local k_FloatingOriginThreshold = 10000.0
local k_CountdownToStartLength = 5.0
local k_CountdownSpeed = 1.5
local k_StartingSegmentDistance = 2.0
local k_StartingSafeSegments = 2
local k_StartingCoinPoolSize = 256
local k_DesiredSegmentCount = 10
local k_SegmentRemovalDistance = -30.0
local k_Acceleration = 0.2

local TrackManager = {}
TrackManager.__index = TrackManager

-- static TrackManager instance { get { return s_Instance; } } -> field.
TrackManager.instance = nil

function TrackManager.new(config)
	local self = setmetatable({}, TrackManager)
	-- Scalar config only; reference fields (characterController, consumableDatabase,
	-- skyMeshFilter, parallaxRoot, tutorialThemeData) are injected before Awake.
	self.minSpeed = config.minSpeed or 5.0
	self.maxSpeed = config.maxSpeed or 10.0
	self.speedStep = config.speedStep or 4
	self.laneOffset = config.laneOffset or 1.0
	self.invincible = config.invincible or false
	self.parallaxRatio = config.parallaxRatio or 0.5

	-- Multicast delegate MultiplierModifier: list of int->int functions
	-- (other scripts register via table.insert(trackManager.modifyMultiply, fn)).
	self.modifyMultiply = nil
	-- System.Action<TrackSegment> hooks.
	self.newSegmentCreated = nil
	self.currentSegementChanged = nil

	-- Public state mirrors (read cross-script as plain fields).
	self.trackSeed = -1
	self.timeToStart = -1.0
	self.score = 0
	self.multiplier = 0
	self.currentSegmentDistance = 0
	self.worldDistance = 0
	self.speed = 0
	self.speedRatio = 0
	self.currentZone = 0
	self.isMoving = false
	self.isRerun = false
	self.isTutorial = false
	self.isLoaded = false
	self.firstObstacle = false
	self.currentTheme = nil

	-- Internal state.
	self.m_ScoreAccum = 0.0
	self.m_TimeSincePowerup = 0.0
	self.m_TimeSinceLastPremium = 0.0
	self.m_CurrentZoneDistance = 0.0
	self.m_PreviousSegment = -1
	self.m_SafeSegementLeft = 0
	self.m_CameraOriginalPos = Vector3.new(0, 0, 0)

	self.m_Segments = {}
	self.m_PastSegments = {}
	self.segments = self.m_Segments -- `segments` property alias (same table ref)

	self._parallaxRootChildren = 0
	self._spawnedSegments = 0

	return self
end

function TrackManager:_updateSpeedRatio()
	self.speedRatio = (self.speed - self.minSpeed) / (self.maxSpeed - self.minSpeed)
end

function TrackManager:Awake()
	self.m_ScoreAccum = 0.0
	TrackManager.instance = self
end

function TrackManager:StartMove(isRestart)
	if isRestart == nil then
		isRestart = true
	end
	self.characterController:StartMoving()
	self.isMoving = true
	if isRestart then
		self.speed = self.minSpeed
		self:_updateSpeedRatio()
	end
end

function TrackManager:StopMove()
	self.isMoving = false
end

-- IEnumerator WaitToStart -> launched via self.host.startCoroutine.
function TrackManager:WaitToStart()
	-- animator.Play(s_StartHash)
	self.characterController.character.gameObject:SetAttribute(s_StartHash, true)
	local length = k_CountdownToStartLength
	self.timeToStart = length

	while self.timeToStart >= 0 do
		local elapsed = task.wait() -- yield return null
		self.timeToStart = self.timeToStart - elapsed * k_CountdownSpeed
	end

	self.timeToStart = -1

	if self.isRerun then
		-- Make invincible on rerun, to avoid problems if the character died in front of an obstacle.
		self.characterController.characterCollider:SetInvincible()
	end

	self.characterController:StartRunning()
	self:StartMove()
end

-- IEnumerator Begin -> launched via self.host.startCoroutine by GameState.
function TrackManager:Begin()
	if not self.isRerun then
		self.firstObstacle = true
		-- Camera.main.transform.position
		self.m_CameraOriginalPos = workspace.CurrentCamera.CFrame.Position

		if self.trackSeed ~= -1 then
			math.randomseed(self.trackSeed)
		else
			math.randomseed(os.time())
		end

		-- Since this is not a rerun, init the whole system.
		self.currentSegmentDistance = k_StartingSegmentDistance
		self.worldDistance = 0.0

		self.host.setActive(self.characterController.gameObject, true)

		-- Addressables.InstantiateAsync(character name) -> host.instantiatePrefab.
		local _pd = PlayerData.instance
		local _charName = _pd.characters[_pd.usedCharacter + 1]
		local playerGO = self.host.instantiatePrefab(_charName, nil, CFrame.new())
		if playerGO == nil then
			warn(string.format("Unable to load character %s.", tostring(_charName)))
			return
		end
		-- op.Result.GetComponent<Character>(): foreign-object component lookup via host.
		local player = self.host.getComponent(playerGO, "Character")

		player:SetupAccesory(_pd.usedAccessory)

		self.characterController.character = player
		self.characterController.trackManager = self

		self.characterController:Init()
		self.characterController:CheatInvincible(self.invincible)

		-- player.transform.SetParent(characterCollider.transform, false)
		playerGO.Parent = self.characterController.characterCollider.gameObject
		-- Camera.main.transform.SetParent(characterController.transform, true):
		-- UNCONVERTED: the Roblox camera is client-owned, not a DataModel child;
		-- reparenting it under a world object has no faithful equivalent.

		if self.isTutorial then
			self.currentTheme = self.tutorialThemeData
		else
			self.currentTheme = ThemeDatabase.GetThemeData(_pd.themes[_pd.usedTheme + 1])
		end

		self.currentZone = 0
		self.m_CurrentZoneDistance = 0

		-- skyMeshFilter.sharedMesh = currentTheme.skyMesh:
		-- UNCONVERTED: MeshFilter swap has no Roblox equivalent (Sky is a Lighting
		-- child built from 6 face textures, not a runtime mesh assignment).
		local _lighting = game:GetService("Lighting")
		_lighting.FogColor = self.currentTheme.fogColor
		-- RenderSettings.fog = true: Roblox fog is always active via FogEnd; nothing to toggle.

		self.host.setActive(self.gameObject, true)
		self.host.setActive(self.characterController.gameObject, true)
		self.characterController.coins = 0
		self.characterController.premium = 0

		self.score = 0
		self.m_ScoreAccum = 0

		self.m_SafeSegementLeft = self.isTutorial and 0 or k_StartingSafeSegments

		Coin.coinPool = Pooler.new(self.currentTheme.collectiblePrefab, k_StartingCoinPoolSize)

		_pd:StartRunMissions(self)

		-- UNITY_ANALYTICS AnalyticsEvent.GameStart omitted (no Roblox equivalent).
	end

	self.characterController:Begin()
	self.host.startCoroutine(self, function()
		self:WaitToStart()
	end)
	self.isLoaded = true
end

function TrackManager:End()
	for _, seg in self.m_Segments do
		self.host.destroy(seg.gameObject) -- Addressables.ReleaseInstance
		self._spawnedSegments = self._spawnedSegments - 1
	end

	for i = 1, #self.m_PastSegments do
		self.host.destroy(self.m_PastSegments[i].gameObject)
	end

	table.clear(self.m_Segments)
	table.clear(self.m_PastSegments)

	self.characterController:End()

	self.host.setActive(self.gameObject, false)
	self.host.destroy(self.characterController.character.gameObject)
	self.characterController.character = nil

	-- Camera.main.transform.SetParent(nil) + position reset: client-owned camera,
	-- no faithful DataModel reparent; restore only the cached original position.
	workspace.CurrentCamera.CFrame = CFrame.new(self.m_CameraOriginalPos)

	self.host.setActive(self.characterController.gameObject, false)

	if self.parallaxRoot ~= nil then
		local _kids = self.parallaxRoot:GetChildren()
		for i = 1, #_kids do
			self._parallaxRootChildren = self._parallaxRootChildren - 1
			self.host.destroy(_kids[i])
		end
	end

	-- If our consumable wasn't used, put it back in our inventory.
	if self.characterController.inventory ~= nil then
		PlayerData.instance:Add(self.characterController.inventory:GetConsumableType())
		self.characterController.inventory = nil
	end
end

function TrackManager:Update(dt)
	while self._spawnedSegments < (self.isTutorial and 4 or k_DesiredSegmentCount) do
		self.host.startCoroutine(self, function()
			self:SpawnNewSegment()
		end)
		self._spawnedSegments = self._spawnedSegments + 1
	end

	if self.parallaxRoot ~= nil and #self.currentTheme.cloudPrefabs > 0 then
		while self._parallaxRootChildren < self.currentTheme.cloudNumber do
			local _kids = self.parallaxRoot:GetChildren()
			local lastZ
			if #_kids == 0 then
				lastZ = 0
			else
				lastZ = _kids[#_kids]:GetPivot().Position.Z + self.currentTheme.cloudMinimumDistance.Z
			end

			local cloud = self.currentTheme.cloudPrefabs[math.random(0, #self.currentTheme.cloudPrefabs - 1) + 1]
			if cloud ~= nil then
				local obj = self.host.instantiatePrefab(cloud, nil, CFrame.new())
				obj.Parent = self.parallaxRoot

				-- localPosition = up*y + forward*z + right*x (Unity forward +Z -> Roblox -Z).
				local cmd = self.currentTheme.cloudMinimumDistance
				local spread = self.currentTheme.cloudSpread
				local lx = cmd.X + (math.random() - 0.5) * spread.X
				local ly = cmd.Y + (math.random() - 0.5) * spread.Y
				local lz = lastZ + (math.random() - 0.5) * spread.Z
				obj:PivotTo(self.parallaxRoot:GetPivot() * CFrame.new(lx, ly, -lz))

				-- localScale *= (1 + (Random.value - 0.5) * 0.5); localRotation = random yaw.
				local _scale = 1.0 + (math.random() - 0.5) * 0.5
				if obj:IsA("Model") then
					obj:ScaleTo(_scale)
				end
				obj:PivotTo(obj:GetPivot() * CFrame.Angles(0, math.rad(math.random() * 360.0), 0))

				self._parallaxRootChildren = self._parallaxRootChildren + 1
			end
		end
	end

	if not self.isMoving then
		return
	end

	local scaledSpeed = self.speed * dt
	self.m_ScoreAccum = self.m_ScoreAccum + scaledSpeed
	self.m_CurrentZoneDistance = self.m_CurrentZoneDistance + scaledSpeed

	local intScore = math.floor(self.m_ScoreAccum)
	if intScore ~= 0 then
		self:AddScore(intScore)
	end
	self.m_ScoreAccum = self.m_ScoreAccum - intScore

	self.worldDistance = self.worldDistance + scaledSpeed
	self.currentSegmentDistance = self.currentSegmentDistance + scaledSpeed

	if self.currentSegmentDistance > self.m_Segments[1].worldLength then
		self.currentSegmentDistance = self.currentSegmentDistance - self.m_Segments[1].worldLength

		-- m_PastSegments are segments we already passed; keep them to move/destroy later.
		table.insert(self.m_PastSegments, self.m_Segments[1])
		table.remove(self.m_Segments, 1)
		self._spawnedSegments = self._spawnedSegments - 1

		if self.currentSegementChanged ~= nil then
			self.currentSegementChanged(self.m_Segments[1])
		end
	end

	local characterTransform = self.characterController.gameObject

	local currentPos, currentRot = self.m_Segments[1]:GetPointAtInWorldUnit(self.currentSegmentDistance)

	-- Floating origin: move the whole world back to 0 when we get too far away.
	local needRecenter = (currentPos.Magnitude * currentPos.Magnitude) > k_FloatingOriginThreshold

	if self.parallaxRoot ~= nil then
		local difference = (currentPos - characterTransform:GetPivot().Position) * self.parallaxRatio
		local _kids = self.parallaxRoot:GetChildren()
		for i = 1, #_kids do
			local cloud = _kids[i]
			local delta = difference - (needRecenter and currentPos or Vector3.new(0, 0, 0))
			cloud:PivotTo(cloud:GetPivot() + delta)
		end
	end

	if needRecenter then
		for i = 1, #self.m_Segments do
			local s = self.m_Segments[i]
			s.gameObject:PivotTo(s.gameObject:GetPivot() + (-currentPos))
		end
		for i = 1, #self.m_PastSegments do
			local s = self.m_PastSegments[i]
			s.gameObject:PivotTo(s.gameObject:GetPivot() + (-currentPos))
		end
		-- Recalculate current world position based on the moved world.
		currentPos, currentRot = self.m_Segments[1]:GetPointAtInWorldUnit(self.currentSegmentDistance)
	end

	-- characterTransform.rotation = currentRot; .position = currentPos.
	characterTransform:PivotTo(currentRot.Rotation + currentPos)

	if self.parallaxRoot ~= nil and #self.currentTheme.cloudPrefabs > 0 then
		local _kids = self.parallaxRoot:GetChildren()
		for i = 1, #_kids do
			local child = _kids[i]
			-- (child.localPosition - currentPos).z < -50: destroy unneeded clouds.
			local childLocal = self.parallaxRoot:GetPivot():ToObjectSpace(child:GetPivot()).Position
			if (childLocal - currentPos).Z < -50 then
				self._parallaxRootChildren = self._parallaxRootChildren - 1
				self.host.destroy(child)
			end
		end
	end

	-- Still move past segments until they aren't visible anymore.
	local i = 1
	while i <= #self.m_PastSegments do
		if (self.m_PastSegments[i].gameObject:GetPivot().Position - currentPos).Z < k_SegmentRemovalDistance then
			self.m_PastSegments[i]:Cleanup()
			table.remove(self.m_PastSegments, i)
		else
			i = i + 1
		end
	end

	self:PowerupSpawnUpdate(dt)

	if not self.isTutorial then
		if self.speed < self.maxSpeed then
			self.speed = self.speed + k_Acceleration * dt
		else
			self.speed = self.maxSpeed
		end
	end

	self:_updateSpeedRatio()

	self.multiplier = 1 + math.floor((self.speed - self.minSpeed) / (self.maxSpeed - self.minSpeed) * self.speedStep)

	if self.modifyMultiply ~= nil then
		for _, part in self.modifyMultiply do
			self.multiplier = part(self.multiplier)
		end
	end

	if not self.isTutorial then
		-- Check for next rank achieved (one milestone every 300m).
		local currentTarget = (PlayerData.instance.rank + 1) * 300
		if self.worldDistance > currentTarget then
			PlayerData.instance.rank = PlayerData.instance.rank + 1
			PlayerData.instance:Save()
			-- UNITY_ANALYTICS AnalyticsEvent.LevelUp omitted.
		end

		PlayerData.instance:UpdateMissions(self)
	end

	MusicPlayer.instance:UpdateVolumes(self.speedRatio)
end

function TrackManager:PowerupSpawnUpdate(dt)
	-- Time.deltaTime sourced from the caller (Update); the higher these go, the
	-- higher the chance of spawning a powerup/premium.
	dt = dt or 0
	self.m_TimeSincePowerup = self.m_TimeSincePowerup + dt
	self.m_TimeSinceLastPremium = self.m_TimeSinceLastPremium + dt
end

function TrackManager:ChangeZone()
	self.currentZone = self.currentZone + 1
	if self.currentZone >= #self.currentTheme.zones then
		self.currentZone = 0
	end

	self.m_CurrentZoneDistance = 0
end

-- IEnumerator SpawnNewSegment -> launched via self.host.startCoroutine.
function TrackManager:SpawnNewSegment()
	if not self.isTutorial then
		if self.currentTheme.zones[self.currentZone + 1].length < self.m_CurrentZoneDistance then
			self:ChangeZone()
		end
	end

	local _zone = self.currentTheme.zones[self.currentZone + 1]
	local segmentUse = math.random(0, #_zone.prefabList - 1)
	if segmentUse == self.m_PreviousSegment then
		segmentUse = (segmentUse + 1) % #_zone.prefabList
	end

	-- InstantiateAsync(prefabList[segmentUse], offScreen, identity) -> host.instantiatePrefab.
	local segGO = self.host.instantiatePrefab(_zone.prefabList[segmentUse + 1], nil, CFrame.new(Vector3.new(-100, -100, -100)))
	if segGO == nil then
		warn("Unable to load segment.")
		return
	end
	local newSegment = self.host.getComponent(segGO, "TrackSegment")

	local currentExitPoint, currentExitRotation
	if #self.m_Segments > 0 then
		currentExitPoint, currentExitRotation = self.m_Segments[#self.m_Segments]:GetPointAt(1.0)
	else
		currentExitPoint = self.gameObject:GetPivot().Position
		currentExitRotation = self.gameObject:GetPivot()
	end

	-- newSegment.transform.rotation = currentExitRotation
	newSegment.gameObject:PivotTo(currentExitRotation.Rotation + newSegment.gameObject:GetPivot().Position)

	local entryPoint, entryRotation = newSegment:GetPointAt(0.0)

	local pos = currentExitPoint + (newSegment.gameObject:GetPivot().Position - entryPoint)
	newSegment.gameObject:PivotTo(newSegment.gameObject:GetPivot().Rotation + pos)
	newSegment.manager = self

	-- newSegment.transform.localScale = new Vector3(±1,1,1) (random mirror) and the
	-- objectRoot inverse-scale compensation:
	-- UNCONVERTED: negative-axis scaling has no Roblox equivalent (ScaleTo is
	-- uniform and positive), so per-segment mirroring is dropped.

	if self.m_SafeSegementLeft <= 0 then
		self:SpawnObstacle(newSegment)
	else
		self.m_SafeSegementLeft = self.m_SafeSegementLeft - 1
	end

	table.insert(self.m_Segments, newSegment)

	if self.newSegmentCreated ~= nil then
		self.newSegmentCreated(newSegment)
	end
end

function TrackManager:SpawnObstacle(segment)
	if #segment.possibleObstacles ~= 0 then
		for posIndex = 0, #segment.obstaclePositions - 1 do
			local assetRef = segment.possibleObstacles[math.random(0, #segment.possibleObstacles - 1) + 1]
			self.host.startCoroutine(self, function()
				self:SpawnFromAssetReference(assetRef, segment, posIndex)
			end)
		end
	end

	self.host.startCoroutine(self, function()
		self:SpawnCoinAndPowerup(segment)
	end)
end

-- IEnumerator SpawnFromAssetReference -> launched via self.host.startCoroutine.
function TrackManager:SpawnFromAssetReference(reference, segment, posIndex)
	-- Addressables.LoadAssetAsync<GameObject>(reference) + obstacle.Spawn coroutine
	-- -> host.instantiatePrefab (best-effort: no load-without-instantiate primitive).
	local obj = self.host.instantiatePrefab(reference, nil, CFrame.new())
	if obj ~= nil then
		local obstacle = self.host.getComponent(obj, "Obstacle")
		if obstacle ~= nil then
			-- Runs synchronously within this coroutine (its own task.wait yields).
			obstacle:Spawn(segment, segment.obstaclePositions[posIndex + 1])
		end
	end
end

-- IEnumerator SpawnCoinAndPowerup -> launched via self.host.startCoroutine.
function TrackManager:SpawnCoinAndPowerup(segment)
	if not self.isTutorial then
		local increment = 1.5
		local currentWorldPos = 0.0
		local currentLane = math.random(0, 2)

		local powerupChance = math.clamp(math.floor(self.m_TimeSincePowerup) * 0.5 * 0.001, 0, 1)
		local premiumChance = math.clamp(math.floor(self.m_TimeSinceLastPremium) * 0.5 * 0.0001, 0, 1)

		while currentWorldPos < segment.worldLength do
			local pos, rot = segment:GetPointAtInWorldUnit(currentWorldPos)

			local laneValid = true
			local testedLane = currentLane
			-- Physics.CheckSphere(pos + laneOffset*(rot*right), 0.4, layer): metres -> studs.
			while #workspace:GetPartBoundsInRadius(pos + ((testedLane - 1) * self.laneOffset * STUDS_PER_METER) * rot.RightVector, 0.4 * STUDS_PER_METER) > 0 do
				testedLane = (testedLane + 1) % 3
				if currentLane == testedLane then
					-- Couldn't find a valid lane.
					laneValid = false
					break
				end
			end

			currentLane = testedLane

			if laneValid then
				pos = pos + ((currentLane - 1) * self.laneOffset * STUDS_PER_METER) * rot.RightVector

				local toUse = nil
				if math.random() < powerupChance then
					local picked = math.random(0, #self.consumableDatabase.consumbales - 1)

					-- If the powerup can't be spawned, don't reset the timer so we keep a high chance next segment.
					if self.consumableDatabase.consumbales[picked + 1].canBeSpawned then
						self.m_TimeSincePowerup = 0.0
						powerupChance = 0.0

						toUse = self.host.instantiatePrefab(self.consumableDatabase.consumbales[picked + 1].gameObject, nil, rot.Rotation + pos)
						if toUse == nil then
							warn("Unable to load consumable.")
							return
						end
						toUse.Parent = segment.gameObject
					end
				elseif math.random() < premiumChance then
					self.m_TimeSinceLastPremium = 0.0
					premiumChance = 0.0

					toUse = self.host.instantiatePrefab(self.currentTheme.premiumCollectible, nil, rot.Rotation + pos)
					if toUse == nil then
						warn("Unable to load collectable.")
						return
					end
					toUse.Parent = segment.gameObject
				else
					toUse = Coin.coinPool:Get(pos, rot.Rotation)
					toUse.Parent = segment.collectibleTransform
				end

				if toUse ~= nil then
					-- TODO hack (#issue7): nudge then restore (net no-op).
					local oldPos = toUse:GetPivot().Position
					toUse:PivotTo(toUse:GetPivot() + Vector3.new(0, 0, 1)) -- Vector3.back -> Roblox +Z
					toUse:PivotTo(toUse:GetPivot().Rotation + oldPos)
				end
			end

			currentWorldPos = currentWorldPos + increment
		end
	end
end

function TrackManager:AddScore(amount)
	local finalAmount = amount
	self.score = self.score + finalAmount * self.multiplier
end

return TrackManager