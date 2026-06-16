local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local MusicPlayer = require(game:GetService("ReplicatedStorage"):FindFirstChild("MusicPlayer", true) or game:GetService("ServerStorage"):FindFirstChild("MusicPlayer", true))
local ThemeDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ThemeDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ThemeDatabase", true))
local CharacterDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("CharacterDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("CharacterDatabase", true))
local ConsumableDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ConsumableDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ConsumableDatabase", true))
local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))
local Modifier = require(game:GetService("ReplicatedStorage"):FindFirstChild("Modifier", true) or game:GetService("ServerStorage"):FindFirstChild("Modifier", true))
local Helpers = require(game:GetService("ReplicatedStorage"):FindFirstChild("Helpers", true) or game:GetService("ServerStorage"):FindFirstChild("Helpers", true))

-- protected const float k_CharacterRotationSpeed = 45f
local k_CharacterRotationSpeed = 45
-- protected const string k_ShopSceneName = "shop"
local k_ShopSceneName = "shop"
-- protected const float k_OwnedAccessoriesCharacterOffset = -0.1f
local k_OwnedAccessoriesCharacterOffset = -0.1

local LoadoutState = {}
LoadoutState.__index = LoadoutState

function LoadoutState.new(config)
	local self = setmetatable({}, LoadoutState)
	-- Pure constructor: C# field initializers only. All serialized reference fields
	-- (inventoryCanvas, the Text/RectTransform/Image UI refs, leaderboard, missionPopup,
	-- runButton, tutorialBlocker/Prompt, skyMeshFilter, UIGroundFilter, the menuTheme
	-- audio id, the consumableIcon prefab) and AState.manager are injected on self by
	-- the host before Enter; never read here.

	self.m_PowerupToUse = Consumable.ConsumableType.NONE

	self.m_Character = nil
	self.m_OwnedAccesories = {} -- new List<int>()
	self.m_UsedAccessory = -1
	self.m_UsedPowerupIndex = 0
	self.m_IsLoadingCharacter = false

	-- protected Modifier m_CurrentModifier = new Modifier(); Modifier.new() is pure.
	self.m_CurrentModifier = Modifier.new()

	-- LayerMask.NameToLayer("UI") has no Roblox equivalent (no per-object camera
	-- layers); tracked inertly and consumed only by the inert Helpers recursion.
	self.k_UILayer = 0

	-- readonly Quaternion k_FlippedYAxisRotation = Quaternion.Euler(0,180,0).
	-- CFrame.Angles is a call, so it is built here (pure) rather than at module scope.
	self.k_FlippedYAxisRotation = CFrame.Angles(0, math.rad(180), 0)

	-- Tick() is driven by GameManager without a dt; derive Time.deltaTime from the
	-- wall clock between successive calls.
	self._lastTick = nil

	return self
end

-- GetComponentInChildren<Text>() on the run button: find its first Text label.
function LoadoutState:_GetButtonLabel(button)
	if button:IsA("TextLabel") or button:IsA("TextButton") then
		return button
	end
	return button:FindFirstChildWhichIsA("TextLabel", true) or button:FindFirstChildWhichIsA("TextButton", true)
end

function LoadoutState:Enter(from)
	self.host.setActive(self.tutorialBlocker, not PlayerData.instance.tutorialDone)
	self.host.setActive(self.tutorialPrompt, false)

	self.host.setActive(self.inventoryCanvas, true)
	self.host.setActive(self.missionPopup.gameObject, false)

	self.charNameDisplay.Text = ""
	self.themeNameDisplay.Text = ""

	-- LayerMask.NameToLayer("UI"): inert (see new()).
	self.k_UILayer = 0

	self.host.setActive(self.skyMeshFilter, true)
	self.host.setActive(self.UIGroundFilter, true)

	-- Shader.SetGlobalFloat("_BlinkingValue", 0.0f): no Roblox global shader-uniform
	-- graph; nothing to reset.

	if MusicPlayer.instance:GetStem(0) ~= self.menuTheme then
		MusicPlayer.instance:SetStem(0, self.menuTheme)
		self.host.startCoroutine(self, function()
			MusicPlayer.instance:RestartAllStems()
		end)
	end

	-- Button.interactable -> closest Roblox GuiButton flag is Active.
	self.runButton.Active = false
	local _label = self:_GetButtonLabel(self.runButton)
	if _label then
		_label.Text = "Loading..."
	end

	if self.m_PowerupToUse ~= Consumable.ConsumableType.NONE then
		-- if we came back from a run and no longer own the wanted powerup, reset to NONE.
		if PlayerData.instance.consumables[self.m_PowerupToUse] == nil
			or PlayerData.instance.consumables[self.m_PowerupToUse] == 0 then
			self.m_PowerupToUse = Consumable.ConsumableType.NONE
		end
	end

	self:Refresh()
end

function LoadoutState:Exit(to)
	self.host.setActive(self.missionPopup.gameObject, false)
	self.host.setActive(self.inventoryCanvas, false)

	if self.m_Character ~= nil then
		self.host.destroy(self.m_Character) -- Addressables.ReleaseInstance
	end

	-- GameState gs = to as GameState: duck-typed on the GameState-only currentModifier
	-- field (covers GameState and its TutorialState subclass).
	local gs = to

	self.host.setActive(self.skyMeshFilter, false)
	self.host.setActive(self.UIGroundFilter, false)

	if gs ~= nil and gs.currentModifier ~= nil then
		gs.currentModifier = self.m_CurrentModifier

		-- Reset to a default modifier for the next run.
		self.m_CurrentModifier = Modifier.new()

		if self.m_PowerupToUse ~= Consumable.ConsumableType.NONE then
			PlayerData.instance:Consume(self.m_PowerupToUse)
			-- Instantiate(ConsumableDatabase.GetConsumbale(...)) cloned the DB template.
			-- Roblox has no host primitive to clone an arbitrary component instance, so
			-- the database consumable is used directly as the inventory item.
			local inv = ConsumableDatabase.GetConsumbale(self.m_PowerupToUse)
			if inv ~= nil then
				self.host.setActive(inv.gameObject, false)
				gs.trackManager.characterController.inventory = inv
			end
		end
	end
end

function LoadoutState:Refresh()
	self:PopulatePowerup()

	self.host.startCoroutine(self, function()
		self:PopulateCharacters()
	end)
	self.host.startCoroutine(self, function()
		self:PopulateTheme()
	end)
end

function LoadoutState:GetName()
	return "Loadout"
end

function LoadoutState:Tick()
	if not self.runButton.Active then
		local interactable = ThemeDatabase.loaded() and CharacterDatabase.loaded()
		if interactable then
			self.runButton.Active = true
			local _label = self:_GetButtonLabel(self.runButton)
			if _label then
				_label.Text = "Run!"
			end

			-- Parent is disabled if the tutorial is already done, so always enable here.
			self.host.setActive(self.tutorialPrompt, true)
		end
	end

	if self.m_Character ~= nil then
		-- m_Character.transform.Rotate(0, k_CharacterRotationSpeed * Time.deltaTime, 0, Space.Self)
		local now = os.clock()
		local dt = self._lastTick and (now - self._lastTick) or 0
		self._lastTick = now
		self.m_Character:PivotTo(self.m_Character:GetPivot() * CFrame.Angles(0, math.rad(k_CharacterRotationSpeed * dt), 0))
	end

	self.host.setActive(self.charSelect, #PlayerData.instance.characters > 1)
	self.host.setActive(self.themeSelect, #PlayerData.instance.themes > 1)
end

function LoadoutState:GoToStore()
	-- UNCONVERTED: Unity additive SceneManager.LoadScene("shop", Additive) has no
	-- Roblox equivalent (no additive scene loading). The shop would be a separate
	-- UI surface / place in Roblox.
end

function LoadoutState:ChangeCharacter(dir)
	PlayerData.instance.usedCharacter = PlayerData.instance.usedCharacter + dir
	if PlayerData.instance.usedCharacter >= #PlayerData.instance.characters then
		PlayerData.instance.usedCharacter = 0
	elseif PlayerData.instance.usedCharacter < 0 then
		PlayerData.instance.usedCharacter = #PlayerData.instance.characters - 1
	end

	self.host.startCoroutine(self, function()
		self:PopulateCharacters()
	end)
end

function LoadoutState:ChangeAccessory(dir)
	self.m_UsedAccessory = self.m_UsedAccessory + dir
	if self.m_UsedAccessory >= #self.m_OwnedAccesories then
		self.m_UsedAccessory = -1
	elseif self.m_UsedAccessory < -1 then
		self.m_UsedAccessory = #self.m_OwnedAccesories - 1
	end

	if self.m_UsedAccessory ~= -1 then
		-- m_OwnedAccesories is 1-based; m_UsedAccessory is a 0-based index into it.
		PlayerData.instance.usedAccessory = self.m_OwnedAccesories[self.m_UsedAccessory + 1]
	else
		PlayerData.instance.usedAccessory = -1
	end

	self:SetupAccessory()
end

function LoadoutState:ChangeTheme(dir)
	PlayerData.instance.usedTheme = PlayerData.instance.usedTheme + dir
	if PlayerData.instance.usedTheme >= #PlayerData.instance.themes then
		PlayerData.instance.usedTheme = 0
	elseif PlayerData.instance.usedTheme < 0 then
		PlayerData.instance.usedTheme = #PlayerData.instance.themes - 1
	end

	self.host.startCoroutine(self, function()
		self:PopulateTheme()
	end)
end

-- IEnumerator PopulateTheme() — launched via self.host.startCoroutine.
function LoadoutState:PopulateTheme()
	local t = nil

	while t == nil do
		-- C# 0-based usedTheme -> Luau 1-based.
		t = ThemeDatabase.GetThemeData(PlayerData.instance.themes[PlayerData.instance.usedTheme + 1])
		task.wait() -- yield return null
	end

	self.themeNameDisplay.Text = t.themeName
	self.themeIcon.Image = t.themeIcon -- themeIcon.sprite

	-- MeshFilter.sharedMesh has no direct Roblox equivalent; if the target is a
	-- MeshPart, assign MeshId, otherwise the swap is inert.
	if self.skyMeshFilter and self.skyMeshFilter:IsA("MeshPart") and t.skyMesh then
		self.skyMeshFilter.MeshId = t.skyMesh
	end
	if self.UIGroundFilter and self.UIGroundFilter:IsA("MeshPart") and t.UIGroundMesh then
		self.UIGroundFilter.MeshId = t.UIGroundMesh
	end
end

-- IEnumerator PopulateCharacters() — launched via self.host.startCoroutine.
function LoadoutState:PopulateCharacters()
	self.host.setActive(self.accessoriesSelector, false)
	PlayerData.instance.usedAccessory = -1
	self.m_UsedAccessory = -1

	if not self.m_IsLoadingCharacter then
		self.m_IsLoadingCharacter = true
		local newChar = nil
		while newChar == nil do
			-- C# 0-based usedCharacter -> Luau 1-based.
			local c = CharacterDatabase.GetCharacter(PlayerData.instance.characters[PlayerData.instance.usedCharacter + 1])

			if c ~= nil then
				table.clear(self.m_OwnedAccesories) -- m_OwnedAccesories.Clear()
				for i = 0, #c.accessories - 1 do
					-- Check which accessories we own.
					local compoundName = c.characterName .. ":" .. c.accessories[i + 1].accessoryName
					if table.find(PlayerData.instance.characterAccessories, compoundName) ~= nil then
						table.insert(self.m_OwnedAccesories, i)
					end
				end

				-- Vector3 pos = charPosition.transform.position; only x is overwritten.
				local pos = self.charPosition:GetPivot().Position
				local newX
				if #self.m_OwnedAccesories > 0 then
					newX = k_OwnedAccessoriesCharacterOffset
				else
					newX = 0.0
				end
				pos = Vector3.new(newX, pos.Y, pos.Z)
				self.charPosition:PivotTo(CFrame.new(pos))

				self.host.setActive(self.accessoriesSelector, #self.m_OwnedAccesories > 0)

				-- Addressables.InstantiateAsync(c.characterName): resolve the character
				-- prefab template by name and instantiate it. The async yield collapses.
				local _templates = game:GetService("ReplicatedStorage"):FindFirstChild("Templates")
				local _template = _templates and _templates:FindFirstChild(c.characterName)
				local op = _template and _template:Clone() or nil
				task.wait() -- yield return op
				if op == nil then
					warn(string.format("Unable to load character %s.", c.characterName))
					return -- yield break
				end
				newChar = op

				Helpers.SetRendererLayerRecursive(newChar, self.k_UILayer)
				newChar.Parent = self.charPosition -- transform.SetParent(charPosition, false)
				-- transform.rotation = k_FlippedYAxisRotation (absolute world rotation).
				newChar:PivotTo(CFrame.new(newChar:GetPivot().Position) * self.k_FlippedYAxisRotation)

				if self.m_Character ~= nil then
					self.host.destroy(self.m_Character)
				end

				self.m_Character = newChar
				self.charNameDisplay.Text = c.characterName

				-- m_Character.transform.localPosition = Vector3.right * 1000 (move off screen).
				self.m_Character:PivotTo(self.charPosition:GetPivot() * CFrame.new(1000, 0, 0) * self.k_FlippedYAxisRotation)
				-- Wait one frame so the rig initialises off-screen (avoids a T-pose flash).
				task.wait() -- yield return new WaitForEndOfFrame()
				-- m_Character.transform.localPosition = Vector3.zero (move back in place).
				self.m_Character:PivotTo(self.charPosition:GetPivot() * self.k_FlippedYAxisRotation)

				self:SetupAccessory()
			else
				task.wait(1.0) -- yield return new WaitForSeconds(1.0f)
			end
		end
		self.m_IsLoadingCharacter = false
	end
end

function LoadoutState:SetupAccessory()
	local c = self.host.getComponent(self.m_Character, "Character")
	c:SetupAccesory(PlayerData.instance.usedAccessory)

	if PlayerData.instance.usedAccessory == -1 then
		self.accesoryNameDisplay.Text = "None"
		self.accessoryIconDisplay.Visible = false -- accessoryIconDisplay.enabled = false
	else
		self.accessoryIconDisplay.Visible = true
		-- C# 0-based usedAccessory -> Luau 1-based.
		self.accesoryNameDisplay.Text = c.accessories[PlayerData.instance.usedAccessory + 1].accessoryName
		self.accessoryIconDisplay.Image = c.accessories[PlayerData.instance.usedAccessory + 1].accessoryIcon
	end
end

function LoadoutState:PopulatePowerup()
	self.host.setActive(self.powerupIcon, true) -- powerupIcon.gameObject.SetActive(true)

	-- consumables is a Dictionary<ConsumableType,int>; count its keys.
	local count = 0
	for _ in pairs(PlayerData.instance.consumables) do
		count = count + 1
	end

	if count > 0 then
		local c = ConsumableDatabase.GetConsumbale(self.m_PowerupToUse)

		self.host.setActive(self.powerupSelect, true)
		if c ~= nil then
			self.powerupIcon.Image = c.icon
			self.powerupCount.Text = tostring(PlayerData.instance.consumables[self.m_PowerupToUse])
		else
			self.powerupIcon.Image = self.noItemIcon
			self.powerupCount.Text = ""
		end
	else
		self.host.setActive(self.powerupSelect, false)
	end
end

function LoadoutState:ChangeConsumable(dir)
	local found = false
	repeat
		self.m_UsedPowerupIndex = self.m_UsedPowerupIndex + dir
		if self.m_UsedPowerupIndex >= Consumable.ConsumableType.MAX_COUNT then
			self.m_UsedPowerupIndex = 0
		elseif self.m_UsedPowerupIndex < 0 then
			self.m_UsedPowerupIndex = Consumable.ConsumableType.MAX_COUNT - 1
		end

		-- TryGetValue((ConsumableType)m_UsedPowerupIndex, out count) && count > 0
		local cnt = PlayerData.instance.consumables[self.m_UsedPowerupIndex]
		if cnt ~= nil and cnt > 0 then
			found = true
		end
		-- C# do { } while (m_UsedPowerupIndex != 0 && !found)
	until self.m_UsedPowerupIndex == 0 or found

	self.m_PowerupToUse = self.m_UsedPowerupIndex
	self:PopulatePowerup()
end

function LoadoutState:UnequipPowerup()
	self.m_PowerupToUse = Consumable.ConsumableType.NONE
end

function LoadoutState:SetModifier(modifier)
	self.m_CurrentModifier = modifier
end

function LoadoutState:StartGame()
	if PlayerData.instance.tutorialDone then
		if PlayerData.instance.ftueLevel == 1 then
			PlayerData.instance.ftueLevel = 2
			PlayerData.instance:Save()
		end
	end

	self.manager:SwitchState("Game")
end

function LoadoutState:Openleaderboard()
	self.leaderboard.displayPlayer = false
	self.leaderboard.forcePlayerDisplay = false
	self.leaderboard:Open()
end

return LoadoutState