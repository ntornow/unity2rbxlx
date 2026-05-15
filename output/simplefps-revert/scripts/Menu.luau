local UserInputService = game:GetService("UserInputService")
local Players = game:GetService("Players")

local container = script.Parent

local function getAudioSource()
	if container:IsA("Instance") then
		return container:FindFirstChildWhichIsA("Sound")
	end
	return nil
end

local startable = false

local function setActiveRecursive(inst, active)
	if inst:IsA("BasePart") then
		inst.Transparency = active and 0 or 1
		inst.CanCollide = active
	end
	if inst:IsA("GuiObject") then
		inst.Visible = active
	end
	for _, child in ipairs(inst:GetChildren()) do
		setActiveRecursive(child, active)
	end
end

local function startGame()
	-- UNCONVERTED: SceneManager.LoadScene(1) — Roblox has no scene loader; use TeleportService or place-level state instead
end

local function toControls()
	local controls = container:FindFirstChild("Controls", true)
	if controls then
		setActiveRecursive(controls, true)
		if controls:IsA("GuiObject") then
			controls.Visible = true
		end
	end
	local source = getAudioSource()
	if source then
		source:Play()
	end
	startable = true
end

local function quitGame()
	local player = Players.LocalPlayer
	if player then
		player:Kick("Quit from menu")
	end
end

local Menu = {}
Menu.ToControls = toControls
Menu.QuitGame = quitGame

do
	UserInputService.MouseIconEnabled = true
	UserInputService.MouseBehavior = Enum.MouseBehavior.Default
end

UserInputService.InputBegan:Connect(function(input, processed)
	if processed then return end
	if not startable then return end
	if input.UserInputType == Enum.UserInputType.MouseButton1
		or input.KeyCode == Enum.KeyCode.ButtonR2
		or input.KeyCode == Enum.KeyCode.Space then
		startGame()
	end
end)

return Menu