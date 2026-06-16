local Menu = {}
Menu.__index = Menu

function Menu.new(config)
    local self = setmetatable({}, Menu)
    self.startable = false
    return self
end

function Menu:getSource()
    return self:GetComponent("AudioSource")
end

function Menu:Awake()
    -- UNCONVERTED: Cursor.visible / Cursor.lockState — Roblox has no per-script cursor lock equivalent here; the mouse is visible by default in menu context.
end

function Menu:Update(dt)
    if self.startable then
        -- Input.GetButtonDown("Fire") — Unity's "Fire" button maps to MouseButton1.
        local UserInputService = game:GetService("UserInputService")
        if UserInputService:IsMouseButtonPressed(Enum.UserInputType.MouseButton1) then
            self:StartGame()
        end
    end
end

function Menu:StartGame()
    -- UNCONVERTED: SceneManager.LoadScene(1) — Roblox has no runtime scene/level loader equivalent; level transitions are place-teleports, not in-place scene swaps.
end

function Menu:ToControls()
    local controls = self.gameObject:FindFirstChild("Controls")
    if controls then
        self.host.setActive(controls, true)
    end
    local source = self:getSource()
    if source then
        source:Play()
    end
    self.startable = true
end

function Menu:QuitGame()
    -- UNCONVERTED: Application.Quit() — Roblox clients cannot terminate the application from a script.
end

return Menu