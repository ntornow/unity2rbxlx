local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local CoroutineHandler = require(game:GetService("ReplicatedStorage"):FindFirstChild("CoroutineHandler", true) or game:GetService("ServerStorage"):FindFirstChild("CoroutineHandler", true))
local CharacterDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("CharacterDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("CharacterDatabase", true))
local ThemeDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ThemeDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ThemeDatabase", true))
local GameManager = require(game:GetService("ReplicatedStorage"):FindFirstChild("GameManager", true) or game:GetService("ServerStorage"):FindFirstChild("GameManager", true))

-- protected const int k_CheatCoins = 1000000
local k_CheatCoins = 1000000
-- protected const int k_CheatPremium = 1000
local k_CheatPremium = 1000

local ShopUI = {}
ShopUI.__index = ShopUI

function ShopUI.new(config)
	local self = setmetatable({}, ShopUI)
	-- Pure constructor: C# field initializers only. The serialized reference fields
	-- (consumableDatabase, itemList, characterList, accessoriesList, themeList, the
	-- coinCounter/premiumCounter Text refs, cheatButton) are injected on self by the
	-- host before Start; never read here.
	self.m_OpenList = nil
	return self
end

function ShopUI:Start()
	PlayerData.Create()

	self.consumableDatabase:Load()
	CoroutineHandler.StartStaticCoroutine(CharacterDatabase.LoadDatabase)
	CoroutineHandler.StartStaticCoroutine(ThemeDatabase.LoadDatabase)

	-- AnalyticsEvent.StoreOpened (UNITY_ANALYTICS) omitted: no Roblox equivalent.

	-- Unity gated cheatButton.interactable on editor/dev-build defines; there is no
	-- Roblox build-flag equivalent, so default to the editor/dev branch (enabled).
	-- Button.interactable -> closest Roblox GuiButton flag is Active.
	self.cheatButton.Active = true

	self.m_OpenList = self.itemList
	self.itemList:Open()
end

function ShopUI:Update(dt)
	self.coinCounter.Text = tostring(PlayerData.instance.coins)
	self.premiumCounter.Text = tostring(PlayerData.instance.premium)
end

function ShopUI:OpenItemList()
	self.m_OpenList:Close()
	self.itemList:Open()
	self.m_OpenList = self.itemList
end

function ShopUI:OpenCharacterList()
	self.m_OpenList:Close()
	self.characterList:Open()
	self.m_OpenList = self.characterList
end

function ShopUI:OpenThemeList()
	self.m_OpenList:Close()
	self.themeList:Open()
	self.m_OpenList = self.themeList
end

function ShopUI:OpenAccessoriesList()
	self.m_OpenList:Close()
	self.accessoriesList:Open()
	self.m_OpenList = self.accessoriesList
end

function ShopUI:LoadScene(scene)
	-- UNCONVERTED: Unity SceneManager.LoadScene(scene, Single) has no Roblox
	-- equivalent (no runtime scene loading). Scenes map to separate places/UI surfaces.
end

function ShopUI:CloseScene()
	-- UNCONVERTED: SceneManager.UnloadSceneAsync("shop") has no Roblox equivalent
	-- (no additive scene unload). The Refresh of the underlying LoadoutState below
	-- is preserved.
	-- GameManager.instance.topState as LoadoutState: duck-typed on the Refresh method
	-- (only LoadoutState exposes it among the AState subclasses).
	local loadoutState = GameManager.instance:getTopState()
	if loadoutState ~= nil and loadoutState.Refresh ~= nil then
		loadoutState:Refresh()
	end
end

function ShopUI:CheatCoin()
	-- Unity returned early on production builds; no Roblox build-flag equivalent, so
	-- the editor/dev cheat path is kept.
	PlayerData.instance.coins = PlayerData.instance.coins + k_CheatCoins
	PlayerData.instance.premium = PlayerData.instance.premium + k_CheatPremium
	PlayerData.instance:Save()
end

-- ShowRewardedAd / HandleShowResult were compiled only under UNITY_ADS (Unity Ads),
-- which is not defined here and has no Roblox equivalent; omitted.

return ShopUI