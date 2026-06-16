local CharacterDatabase = {}

local m_CharactersDict = nil
local m_Loaded = false

function CharacterDatabase.dictionary()
	return m_CharactersDict
end

function CharacterDatabase.loaded()
	return m_Loaded
end

function CharacterDatabase.GetCharacter(type)
	if m_CharactersDict == nil then
		return nil
	end
	local c = m_CharactersDict[type]
	if c == nil then
		return nil
	end
	return c
end

function CharacterDatabase.LoadDatabase()
	if m_CharactersDict == nil then
		m_CharactersDict = {}

		-- Unity loaded character prefabs via Addressables.LoadAssetsAsync("characters", ...).
		-- Roblox has no Addressables; resolve the equivalent set of Character prefabs from
		-- a known container (ReplicatedStorage/Characters) and index them by characterName.
		local ReplicatedStorage = game:GetService("ReplicatedStorage")
		local container = ReplicatedStorage:FindFirstChild("Characters")
		if container then
			for _, op in ipairs(container:GetChildren()) do
				local Character = require(script.Parent.Character)
				local c = Character.GetComponent and Character.GetComponent(op) or op
				if c ~= nil then
					local characterName = op:GetAttribute("characterName") or op.Name
					m_CharactersDict[characterName] = c
				end
			end
		end

		m_Loaded = true
	end
end

return CharacterDatabase