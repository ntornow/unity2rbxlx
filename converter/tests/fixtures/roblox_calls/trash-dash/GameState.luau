local MusicPlayer = require(game:GetService("ReplicatedStorage"):FindFirstChild("MusicPlayer", true) or game:GetService("ServerStorage"):FindFirstChild("MusicPlayer", true))
local CoroutineHandler = require(game:GetService("ReplicatedStorage"):FindFirstChild("CoroutineHandler", true) or game:GetService("ServerStorage"):FindFirstChild("CoroutineHandler", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local Modifier = require(game:GetService("ReplicatedStorage"):FindFirstChild("Modifier", true) or game:GetService("ServerStorage"):FindFirstChild("Modifier", true))

-- static int s_DeadHash = Animator.StringToHash("Dead") -> attribute name.
local s_DeadHash = "Dead"

local k_MaxLives = 3
local k_ObstacleToClear = 3

local GameState = {}
GameState.__index = GameState

function GameState.new(config)
	local self = setmetatable({}, GameState)
	-- Pure constructor: scalar config only. Reference fields (canvas, trackManager,
	-- all UI Text/RectTransform/Image/Button/GameObject refs), the audio asset
	-- gameTheme, the prefab field PowerupIconPrefab, and AState.manager are injected
	-- on self by the host before they are used. C# field initializers (currentModifier,
	-- adsPlacementId, m_PowerupIcons, ...) are evaluated HERE, never at module scope.

	self.adsPlacementId = config.adsPlacementId or "rewardedVideo"
	if config.adsRewarded ~= nil then
		self.adsRewarded = config.adsRewarded
	else
		self.adsRewarded = true
	end

	-- public Modifier currentModifier = new Modifier(); Modifier.new() is pure.
	self.currentModifier = Modifier.new()

	self.m_Finished = false
	self.m_TimeSinceStart = 0
	self.m_PowerupIcons = {}
	self.m_LifeHearts = {}

	self.m_CountdownRectTransform = nil
	self.m_WasMoving = false

	self.m_AdsInitialised = false
	self.m_GameoverSelectionDone = false

	self.m_IsTutorial = false
	self.m_TutorialClearedObstacle = 0
	self.m_CountObstacles = true
	self.m_DisplayTutorial = false
	self.m_CurrentSegmentObstacleIndex = 0
	self.m_NextValidSegment = nil

	-- AudioListener.pause is a Unity global with no Roblox equivalent; tracked locally.
	self.m_AudioPaused = false

	-- Tick() is driven by GameManager without a dt; derive Time.deltaTime from the
	-- wall clock between successive calls.
	self._lastTick = nil

	return self
end

function GameState:Enter(from)
	-- countdownText.GetComponent<RectTransform>(): in Roblox the GUI object itself
	-- carries layout, so the RectTransform aliases the countdownText instance.
	self.m_CountdownRectTransform = self.countdownText

	self.m_LifeHearts = {}
	for i = 0, k_MaxLives - 1 do
		-- lifeRectTransform.GetChild(i).GetComponent<Image>() -> i-th child ImageLabel.
		self.m_LifeHearts[i + 1] = self.lifeRectTransform:GetChildren()[i + 1]
	end

	if MusicPlayer.instance:GetStem(0) ~= self.gameTheme then
		MusicPlayer.instance:SetStem(0, self.gameTheme)
		CoroutineHandler.StartStaticCoroutine(function()
			MusicPlayer.instance:RestartAllStems()
		end)
	end

	self.m_AdsInitialised = false
	self.m_GameoverSelectionDone = false

	self:StartGame()
end

function GameState:Exit(to)
	self.host.setActive(self.canvas, false) -- canvas.gameObject.SetActive(false)

	self:ClearPowerup()
end

function GameState:StartGame()
	self.host.setActive(self.canvas, true)
	self.host.setActive(self.pauseMenu, false)
	self.host.setActive(self.wholeUI, true)
	self.host.setActive(self.pauseButton, not self.trackManager.isTutorial)
	self.host.setActive(self.gameOverPopup, false)

	self.host.setActive(self.sideSlideTuto, false)
	self.host.setActive(self.upSlideTuto, false)
	self.host.setActive(self.downSlideTuto, false)
	self.host.setActive(self.finishTuto, false)
	self.host.setActive(self.tutorialValidatedObstacles, false)

	if not self.trackManager.isRerun then
		self.m_TimeSinceStart = 0
		self.trackManager.characterController.currentLife = self.trackManager.characterController.maxLife
	end

	self.currentModifier:OnRunStart(self)

	self.m_IsTutorial = not PlayerData.instance.tutorialDone
	self.trackManager.isTutorial = self.m_IsTutorial

	if self.m_IsTutorial then
		self.host.setActive(self.tutorialValidatedObstacles, true)
		self.tutorialValidatedObstacles.Text = "0/" .. tostring(k_ObstacleToClear)

		self.m_DisplayTutorial = true
		self.trackManager.newSegmentCreated = function(segment)
			if self.trackManager.currentZone ~= 0 and not self.m_CountObstacles and self.m_NextValidSegment == nil then
				self.m_NextValidSegment = segment
			end
		end

		self.trackManager.currentSegementChanged = function(segment)
			self.m_CurrentSegmentObstacleIndex = 0

			if not self.m_CountObstacles and self.trackManager.currentSegment == self.m_NextValidSegment then
				self.trackManager.characterController.currentTutorialLevel = self.trackManager.characterController.currentTutorialLevel + 1
				self.m_CountObstacles = true
				self.m_NextValidSegment = nil
				self.m_DisplayTutorial = true

				self.tutorialValidatedObstacles.Text = tostring(self.m_TutorialClearedObstacle) .. "/" .. tostring(k_ObstacleToClear)
			end
		end
	end

	self.m_Finished = false
	table.clear(self.m_PowerupIcons)

	self.host.startCoroutine(self, function()
		self.trackManager:Begin()
	end)
end

function GameState:GetName()
	return "Game"
end

function GameState:Tick()
	if self.m_Finished then
		-- UNITY_ADS disabled build path: adsForLifeButton.SetActive(false).
		self.host.setActive(self.adsForLifeButton, false)
		return
	end

	if self.trackManager.isLoaded then
		local chrCtrl = self.trackManager.characterController

		-- m_TimeSinceStart += Time.deltaTime (derived from wall clock; see new()).
		local now = os.clock()
		local dt = self._lastTick and (now - self._lastTick) or 0
		self._lastTick = now
		self.m_TimeSinceStart = self.m_TimeSinceStart + dt

		if chrCtrl.currentLife <= 0 then
			self.host.setActive(self.pauseButton, false)
			chrCtrl:CleanConsumable()
			chrCtrl.character.gameObject:SetAttribute(s_DeadHash, true) -- animator.SetBool(Dead, true)
			self.host.setActive(chrCtrl.characterCollider.koParticle, true)
			self.host.startCoroutine(self, function()
				self:WaitForGameOver()
			end)
		end

		-- Consumable ticking & lifetime management. toRemove / toRemoveIcon are
		-- parallel arrays (an icon entry may be nil), so index explicitly rather
		-- than table.insert (which would drop nils and desync the indices).
		local toRemove = {}
		local toRemoveIcon = {}
		local removeCount = 0

		for i = 1, #chrCtrl.consumables do
			local icon = nil
			for j = 1, #self.m_PowerupIcons do
				if self.m_PowerupIcons[j].linkedConsumable == chrCtrl.consumables[i] then
					icon = self.m_PowerupIcons[j]
					break
				end
			end

			chrCtrl.consumables[i]:Tick(chrCtrl)
			if not chrCtrl.consumables[i]:active() then
				removeCount = removeCount + 1
				toRemove[removeCount] = chrCtrl.consumables[i]
				toRemoveIcon[removeCount] = icon
			elseif icon == nil then
				-- If there's no icon for the active consumable, create it!
				local o = self.host.instantiatePrefab(self.PowerupIconPrefab, nil, CFrame.new())

				icon = self.host.getComponent(o, "PowerupIcon")

				icon.linkedConsumable = chrCtrl.consumables[i]
				o.Parent = self.powerupZone -- icon.transform.SetParent(powerupZone, false)

				table.insert(self.m_PowerupIcons, icon)
			end
		end

		for i = 1, removeCount do
			toRemove[i]:Ended(self.trackManager.characterController)

			self.host.destroy(toRemove[i].gameObject) -- Addressables.ReleaseInstance
			if toRemoveIcon[i] ~= nil then
				self.host.destroy(toRemoveIcon[i].gameObject)
			end

			local ci = table.find(chrCtrl.consumables, toRemove[i])
			if ci ~= nil then
				table.remove(chrCtrl.consumables, ci)
			end
			local pi = table.find(self.m_PowerupIcons, toRemoveIcon[i])
			if pi ~= nil then
				table.remove(self.m_PowerupIcons, pi)
			end
		end

		if self.m_IsTutorial then
			self:TutorialCheckObstacleClear()
		end

		self:UpdateUI()

		self.currentModifier:OnRunTick(self)
	end
end

function GameState:OnApplicationPause(pauseStatus)
	-- UNCONVERTED: Unity application-pause callback has no Roblox host event; pause
	-- is driven through the UI pause button (Pause()) instead.
end

function GameState:OnApplicationFocus(focusStatus)
	-- UNCONVERTED: Unity application-focus callback has no Roblox host event.
end

function GameState:Pause(displayMenu)
	if displayMenu == nil then
		displayMenu = true
	end

	-- check if we aren't finished OR if we aren't already in pause.
	if self.m_Finished or self.m_AudioPaused == true then
		return
	end

	self.m_AudioPaused = true -- AudioListener.pause = true
	-- Time.timeScale = 0: no Roblox equivalent (engine has no global time scale).

	self.host.setActive(self.pauseButton, false)
	self.host.setActive(self.pauseMenu, displayMenu)
	self.host.setActive(self.wholeUI, false)
	self.m_WasMoving = self.trackManager.isMoving
	self.trackManager:StopMove()
end

function GameState:Resume()
	-- Time.timeScale = 1.0f: no Roblox equivalent.
	self.host.setActive(self.pauseButton, true)
	self.host.setActive(self.pauseMenu, false)
	self.host.setActive(self.wholeUI, true)
	if self.m_WasMoving then
		self.trackManager:StartMove(false)
	end

	self.m_AudioPaused = false -- AudioListener.pause = false
end

function GameState:QuitToLoadout()
	-- Used by the pause menu to return immediately to loadout, canceling everything.
	-- Time.timeScale = 1.0f: no Roblox equivalent.
	self.m_AudioPaused = false -- AudioListener.pause = false
	self.trackManager:End()
	self.trackManager.isRerun = false
	PlayerData.instance:Save()
	self.manager:SwitchState("Loadout")
end

function GameState:UpdateUI()
	self.coinText.Text = tostring(self.trackManager.characterController.coins)
	self.premiumText.Text = tostring(self.trackManager.characterController.premium)

	for i = 0, 2 do
		if self.trackManager.characterController.currentLife > i then
			self.m_LifeHearts[i + 1].ImageColor3 = Color3.new(1, 1, 1) -- Color.white
		else
			self.m_LifeHearts[i + 1].ImageColor3 = Color3.new(0, 0, 0) -- Color.black
		end
	end

	self.scoreText.Text = tostring(self.trackManager.score)
	self.multiplierText.Text = "x " .. tostring(self.trackManager.multiplier)

	self.distanceText.Text = tostring(math.floor(self.trackManager.worldDistance)) .. "m"

	if self.trackManager.timeToStart >= 0 then
		self.host.setActive(self.countdownText, true)
		self.countdownText.Text = tostring(math.ceil(self.trackManager.timeToStart))
		-- m_CountdownRectTransform.localScale = Vector3.one * (1 - frac(timeToStart)).
		-- RectTransform.localScale has no direct GuiObject property; apply to a
		-- UIScale child if one is present (the countdown "pop" animation).
		local _scale = 1.0 - (self.trackManager.timeToStart - math.floor(self.trackManager.timeToStart))
		local _uiScale = self.m_CountdownRectTransform:FindFirstChildOfClass("UIScale")
		if _uiScale then
			_uiScale.Scale = _scale
		end
	else
		-- m_CountdownRectTransform.localScale = Vector3.zero.
		local _uiScale = self.m_CountdownRectTransform:FindFirstChildOfClass("UIScale")
		if _uiScale then
			_uiScale.Scale = 0
		end
	end

	-- Consumable inventory icon.
	if self.trackManager.characterController.inventory ~= nil then
		self.host.setActive(self.inventoryIcon.Parent, true) -- inventoryIcon.transform.parent.gameObject
		self.inventoryIcon.Image = self.trackManager.characterController.inventory.icon -- inventoryIcon.sprite
	else
		self.host.setActive(self.inventoryIcon.Parent, false)
	end
end

-- IEnumerator WaitForGameOver -> launched via self.host.startCoroutine.
function GameState:WaitForGameOver()
	self.m_Finished = true
	self.trackManager:StopMove()

	-- Shader.SetGlobalFloat("_BlinkingValue", 0): no Roblox global shader-uniform
	-- equivalent (no global shader parameter graph).

	task.wait(2.0) -- yield return new WaitForSeconds(2.0f)
	if self.currentModifier:OnRunEnd(self) then
		if self.trackManager.isRerun then
			self.manager:SwitchState("GameOver")
		else
			self:OpenGameOverPopup()
		end
	end
end

function GameState:ClearPowerup()
	for i = 1, #self.m_PowerupIcons do
		if self.m_PowerupIcons[i] ~= nil then
			self.host.destroy(self.m_PowerupIcons[i].gameObject)
		end
	end

	self.trackManager.characterController.powerupSource:Stop()

	table.clear(self.m_PowerupIcons)
end

function GameState:OpenGameOverPopup()
	-- Button.interactable -> closest Roblox GuiButton flag is Active.
	self.premiumForLifeButton.Active = PlayerData.instance.premium >= 3

	self.premiumCurrencyOwned.Text = tostring(PlayerData.instance.premium)

	self:ClearPowerup()

	self.host.setActive(self.gameOverPopup, true)
end

function GameState:GameOver()
	self.manager:SwitchState("GameOver")
end

function GameState:PremiumForLife()
	-- Avoid a bug where the video AND premium button release on the same frame.
	if self.m_GameoverSelectionDone then
		return
	end

	self.m_GameoverSelectionDone = true

	PlayerData.instance.premium = PlayerData.instance.premium - 3
	self.trackManager.characterController.premium = self.trackManager.characterController.premium
		- math.min(self.trackManager.characterController.premium, 3)

	self:SecondWind()
end

function GameState:SecondWind()
	self.trackManager.characterController.currentLife = 1
	self.trackManager.isRerun = true
	self:StartGame()
end

function GameState:ShowRewardedAd()
	if self.m_GameoverSelectionDone then
		return
	end

	self.m_GameoverSelectionDone = true

	-- UNITY_ADS not defined in this build: fall through directly to GameOver().
	self:GameOver()
end

function GameState:HandleShowResult(result)
	-- UNCONVERTED: Unity Ads ShowResult callback (UnityEngine.Advertisements has no
	-- Roblox equivalent). Only compiled under UNITY_ADS, which is not defined here.
end

function GameState:TutorialCheckObstacleClear()
	if #self.trackManager.segments == 0 then
		return
	end

	if self.m_AudioPaused and not self.trackManager.characterController.tutorialWaitingForValidation then
		self.m_DisplayTutorial = false
		self:DisplayTutorial(false)
	end

	local ratio = self.trackManager.currentSegmentDistance / self.trackManager.currentSegment.worldLength
	local nextObstaclePosition
	if self.m_CurrentSegmentObstacleIndex < #self.trackManager.currentSegment.obstaclePositions then
		nextObstaclePosition = self.trackManager.currentSegment.obstaclePositions[self.m_CurrentSegmentObstacleIndex + 1]
	else
		nextObstaclePosition = math.huge -- float.MaxValue
	end

	if self.m_CountObstacles and ratio > nextObstaclePosition + 0.05 then
		self.m_CurrentSegmentObstacleIndex = self.m_CurrentSegmentObstacleIndex + 1

		if not self.trackManager.characterController.characterCollider.tutorialHitObstacle then
			self.m_TutorialClearedObstacle = self.m_TutorialClearedObstacle + 1
			self.tutorialValidatedObstacles.Text = tostring(self.m_TutorialClearedObstacle) .. "/" .. tostring(k_ObstacleToClear)
		end

		self.trackManager.characterController.characterCollider.tutorialHitObstacle = false

		if self.m_TutorialClearedObstacle == k_ObstacleToClear then
			self.m_TutorialClearedObstacle = 0
			self.m_CountObstacles = false
			self.m_NextValidSegment = nil
			self.trackManager:ChangeZone()

			self.tutorialValidatedObstacles.Text = "Passed!"

			if self.trackManager.currentZone == 0 then
				-- we looped, meaning we finished the tutorial.
				self.trackManager.characterController.currentTutorialLevel = 3
				self:DisplayTutorial(true)
			end
		end
	elseif self.m_DisplayTutorial and ratio > nextObstaclePosition - 0.1 then
		self:DisplayTutorial(true)
	end
end

function GameState:DisplayTutorial(value)
	if value then
		self:Pause(false)
	else
		self:Resume()
	end

	local lvl = self.trackManager.characterController.currentTutorialLevel
	if lvl == 0 then
		self.host.setActive(self.sideSlideTuto, value)
		self.trackManager.characterController.tutorialWaitingForValidation = value
	elseif lvl == 1 then
		self.host.setActive(self.upSlideTuto, value)
		self.trackManager.characterController.tutorialWaitingForValidation = value
	elseif lvl == 2 then
		self.host.setActive(self.downSlideTuto, value)
		self.trackManager.characterController.tutorialWaitingForValidation = value
	elseif lvl == 3 then
		self.host.setActive(self.finishTuto, value)
		self.trackManager.characterController:StopSliding()
		self.trackManager.characterController.tutorialWaitingForValidation = value
	end
end

function GameState:FinishTutorial()
	PlayerData.instance.tutorialDone = true
	PlayerData.instance:Save()

	self:QuitToLoadout()
end

return GameState