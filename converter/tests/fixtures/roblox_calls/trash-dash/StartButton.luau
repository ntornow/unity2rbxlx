local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local StartButton = {}
StartButton.__index = StartButton

function StartButton.new(config)
    local self = setmetatable({}, StartButton)
    return self
end

function StartButton:StartGame()
    if PlayerData.instance.ftueLevel == 0 then
        PlayerData.instance.ftueLevel = 1
        PlayerData.instance:Save()
        -- UNITY_ANALYTICS AnalyticsEvent.FirstInteraction("start_button_pressed") omitted (no Roblox equivalent).
    end

    -- UNITY_PURCHASING StandardPurchasingModule.Instance() omitted (no Roblox equivalent).

    -- SceneManager.LoadScene("main") has no Roblox equivalent: a converted game is
    -- a single place, not separately-loadable Unity scenes. Signal the game-state
    -- transition the original scene load implied so the boot/state layer can react.
    local _gm = self.host.findObjectOfType("GameManager")
    if _gm then
        self.host.setActive(self.gameObject, false)
    end
end

return StartButton