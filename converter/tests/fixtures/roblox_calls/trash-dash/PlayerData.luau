local Missions = require(script.Parent.Missions)
local CharacterDatabase = require(script.Parent.CharacterDatabase)
local ThemeDatabase = require(script.Parent.ThemeDatabase)
local CoroutineHandler = require(script.Parent.CoroutineHandler)

-- struct HighscoreEntry : IComparable<HighscoreEntry>
-- Plain table { name = "", score = 0 }. CompareTo (sort highest->lowest) is
-- inlined into the binary search in GetScorePlace below.

local PlayerData = {}
PlayerData.__index = PlayerData

-- static protected PlayerData m_Instance / static public instance getter.
-- Accessed as a field (PlayerData.instance) by other scripts.
PlayerData.instance = nil

-- static int s_Version = 12
local s_Version = 12

-- float.MinValue sentinel meaning "volume not yet set".
local FLOAT_MIN = -math.huge

function PlayerData.new()
	local self = setmetatable({}, PlayerData)

	self.saveFile = ""

	self.coins = 0
	self.premium = 0
	self.consumables = {} -- keyed by Consumable.ConsumableType -> count

	self.characters = {}
	self.usedCharacter = 0
	self.usedAccessory = -1
	self.characterAccessories = {}
	self.themes = {}
	self.usedTheme = 0
	self.highscores = {}
	self.missions = {}

	self.previousName = "Trash Cat"

	self.licenceAccepted = false
	self.tutorialDone = false

	self.masterVolume = FLOAT_MIN
	self.musicVolume = FLOAT_MIN
	self.masterSFXVolume = FLOAT_MIN

	self.ftueLevel = 0
	self.rank = 0

	return self
end

function PlayerData:Consume(type)
	if self.consumables[type] == nil then
		return
	end

	self.consumables[type] = self.consumables[type] - 1
	if self.consumables[type] == 0 then
		self.consumables[type] = nil
	end

	self:Save()
end

function PlayerData:Add(type)
	if self.consumables[type] == nil then
		self.consumables[type] = 0
	end

	self.consumables[type] = self.consumables[type] + 1

	self:Save()
end

function PlayerData:AddCharacter(name)
	table.insert(self.characters, name)
end

function PlayerData:AddTheme(theme)
	table.insert(self.themes, theme)
end

function PlayerData:AddAccessory(name)
	table.insert(self.characterAccessories, name)
end

-- Mission management

function PlayerData:CheckMissionsCount()
	while #self.missions < 2 do
		self:AddMission()
	end
end

function PlayerData:AddMission()
	-- Random.Range(0, (int)MissionType.MAX) -> int in [0, MAX-1]
	local val = math.random(0, Missions.MissionType.MAX - 1)

	local newMission = Missions.GetNewMissionFromType(val)
	newMission:Created()

	table.insert(self.missions, newMission)
end

function PlayerData:StartRunMissions(manager)
	for i = 1, #self.missions do
		self.missions[i]:RunStart(manager)
	end
end

function PlayerData:UpdateMissions(manager)
	for i = 1, #self.missions do
		self.missions[i]:Update(manager)
	end
end

function PlayerData:AnyMissionComplete()
	for i = 1, #self.missions do
		if self.missions[i]:isComplete() then
			return true
		end
	end

	return false
end

function PlayerData:ClaimMission(mission)
	self.premium = self.premium + mission.reward

	-- UNITY_ANALYTICS AnalyticsEvent.ItemAcquired block omitted (no Roblox equivalent).

	local idx = table.find(self.missions, mission)
	if idx ~= nil then
		table.remove(self.missions, idx)
	end

	self:CheckMissionsCount()

	self:Save()
end

-- High Score management

function PlayerData:GetScorePlace(score)
	-- highscores is kept sorted highest->lowest (HighscoreEntry.CompareTo inverts).
	-- Mirror List.BinarySearch: return the found 0-based index, otherwise the
	-- 0-based insertion point (matching the C# `~index` unfold).
	local lo = 0
	local hi = #self.highscores - 1

	while lo <= hi do
		local mid = math.floor((lo + hi) / 2)
		local midScore = self.highscores[mid + 1].score

		if midScore == score then
			return mid
		elseif midScore > score then
			lo = mid + 1
		else
			hi = mid - 1
		end
	end

	return lo
end

function PlayerData:InsertScore(score, name)
	local entry = { name = name, score = score }

	local place = self:GetScorePlace(score)
	table.insert(self.highscores, place + 1, entry)

	-- Keep only the 10 best scores.
	while #self.highscores > 10 do
		table.remove(self.highscores, #self.highscores)
	end
end

-- File management

function PlayerData.Create()
	if PlayerData.instance == nil then
		PlayerData.instance = PlayerData.new()

		-- First call: init the databases as early as possible.
		CoroutineHandler.StartStaticCoroutine(CharacterDatabase.LoadDatabase)
		CoroutineHandler.StartStaticCoroutine(ThemeDatabase.LoadDatabase)
	end

	-- Application.persistentDataPath has no Roblox equivalent (no local FS).
	PlayerData.instance.saveFile = "save.bin"

	-- File.Exists / local save.bin persistence has no Roblox equivalent, so the
	-- "load existing save" branch can never run; always start a fresh save.
	-- (Real persistence would use DataStoreService, async + per-player key.)
	PlayerData.NewSave()

	PlayerData.instance:CheckMissionsCount()
end

function PlayerData.NewSave()
	local self = PlayerData.instance

	self.characters = {}
	self.themes = {}
	self.missions = {}
	self.characterAccessories = {}
	self.consumables = {}

	self.usedCharacter = 0
	self.usedTheme = 0
	self.usedAccessory = -1

	self.coins = 0
	self.premium = 0

	table.insert(self.characters, "Trash Cat")
	table.insert(self.themes, "Day")

	self.ftueLevel = 0
	self.rank = 0

	self:CheckMissionsCount()

	self:Save()
end

function PlayerData:Read()
	-- UNCONVERTED: BinaryReader/FileStream local-file deserialization has no
	-- Roblox equivalent. Roblox would load persisted state via DataStoreService
	-- (async, per-player key) rather than reading a local save.bin byte stream.
	-- Create() never reaches this path (no local file exists); kept as a no-op
	-- so any direct caller continues to run.
end

function PlayerData:Save()
	-- UNCONVERTED: BinaryWriter/FileStream local-file persistence has no Roblox
	-- equivalent. Roblox would persist via DataStoreService (async, per-player
	-- key) rather than writing a local save.bin. Left as a no-op so callers that
	-- invoke Save() after mutating in-memory state continue to run.
end

-- PlayerDataEditor (UNITY_EDITOR debug menu items) omitted: editor-only tooling
-- with no Roblox equivalent.

return PlayerData