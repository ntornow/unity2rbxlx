local GameManager = {}
GameManager.__index = GameManager

function GameManager.new(config)
    local self = setmetatable({}, GameManager)
    return self
end

function GameManager:Awake()
    local UserInputService = game:GetService("UserInputService")
    UserInputService.MouseIconEnabled = false
    UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
end

function GameManager:RestartGame(time)
    self.host.startCoroutine(self, function()
        self:RestartTimer(time)
    end)
end

function GameManager:RestartTimer(time)
    task.wait(time)
    -- UNCONVERTED: SceneManager.LoadScene(0) — Roblox has no scene-reload API; place reload requires TeleportService:Teleport to the same place, which is a deploy-time decision, not a faithful in-engine translation.
end

return GameManager