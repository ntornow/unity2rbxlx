local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local MusicPlayer = require(game:GetService("ReplicatedStorage"):FindFirstChild("MusicPlayer", true) or game:GetService("ServerStorage"):FindFirstChild("MusicPlayer", true))

-- GameOverState : AState. AState is an abstract MonoBehaviour whose Enter/Exit/
-- Tick/GetName contract the GameManager drives; `self.manager` is injected on
-- this state by GameManager:OnEnable before any call. Reference fields
-- (trackManager, canvas, missionPopup, miniLeaderboard, fullLeaderboard,
-- addButton) and the gameOverTheme audio asset (rbxassetid string) are injected
-- on self by the host before use; never read here.

local GameOverState = {}
GameOverState.__index = GameOverState

function GameOverState.new(config)
	local self = setmetatable({}, GameOverState)
	-- No scalar config fields; everything is an injected reference/asset field.
	return self
end

-- public override void Enter(AState from)
function GameOverState:Enter(from)
	self.host.setActive(self.canvas.gameObject, true)

	self.miniLeaderboard.playerEntry.inputName.Text = PlayerData.instance.previousName

	self.miniLeaderboard.playerEntry.score.Text = tostring(self.trackManager.score)
	self.miniLeaderboard:Populate()

	if PlayerData.instance:AnyMissionComplete() then
		self.host.startCoroutine(self, function()
			self.missionPopup:Open()
		end)
	else
		self.host.setActive(self.missionPopup.gameObject, false)
	end

	self:CreditCoins()

	if MusicPlayer.instance:GetStem(0) ~= self.gameOverTheme then
		MusicPlayer.instance:SetStem(0, self.gameOverTheme)
		self.host.startCoroutine(self, function()
			MusicPlayer.instance:RestartAllStems()
		end)
	end
end

-- public override void Exit(AState to)
function GameOverState:Exit(to)
	self.host.setActive(self.canvas.gameObject, false)
	self:FinishRun()
end

-- public override string GetName()
function GameOverState:GetName()
	return "GameOver"
end

-- public override void Tick()
function GameOverState:Tick()
	-- Empty in source.
end

-- public void OpenLeaderboard()
function GameOverState:OpenLeaderboard()
	self.fullLeaderboard.forcePlayerDisplay = false
	self.fullLeaderboard.displayPlayer = true
	self.fullLeaderboard.playerEntry.playerName.Text = self.miniLeaderboard.playerEntry.inputName.Text
	self.fullLeaderboard.playerEntry.score.Text = tostring(self.trackManager.score)

	self.fullLeaderboard:Open()
end

-- public void GoToStore()
function GameOverState:GoToStore()
	-- UNCONVERTED: SceneManager.LoadScene("shop", Additive) — Roblox has no
	-- additive multi-scene loading; a shop would be a separate ScreenGui/place,
	-- not a runtime-merged scene.
end

-- public void GoToLoadout()
function GameOverState:GoToLoadout()
	self.trackManager.isRerun = false
	self.manager:SwitchState("Loadout")
end

-- public void RunAgain()
function GameOverState:RunAgain()
	self.trackManager.isRerun = false
	self.manager:SwitchState("Game")
end

-- protected void CreditCoins()
function GameOverState:CreditCoins()
	PlayerData.instance:Save()

	-- UNITY_ANALYTICS AnalyticsEvent.ItemAcquired (soft "fishbone" / premium
	-- "anchovies" currency) block omitted — no Roblox analytics equivalent.
end

-- protected void FinishRun()
function GameOverState:FinishRun()
	if self.miniLeaderboard.playerEntry.inputName.Text == "" then
		self.miniLeaderboard.playerEntry.inputName.Text = "Trash Cat"
	else
		PlayerData.instance.previousName = self.miniLeaderboard.playerEntry.inputName.Text
	end

	PlayerData.instance:InsertScore(self.trackManager.score, self.miniLeaderboard.playerEntry.inputName.Text)

	-- CharacterCollider.DeathEvent de = ...characterCollider.deathData fed only the
	-- omitted UNITY_ANALYTICS AnalyticsEvent.GameOver event; no Roblox equivalent.

	PlayerData.instance:Save()

	self.trackManager:End()
end

return GameOverState