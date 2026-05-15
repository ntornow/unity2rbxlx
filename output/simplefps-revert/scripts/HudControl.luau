-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

-- Services
local Players = game:GetService("Players")

local localPlayer = Players.LocalPlayer
local playerGui = localPlayer:WaitForChild("PlayerGui")

-- Canvas in Unity → ScreenGui in Roblox. This script is parented to that
-- ScreenGui; fall back to PlayerGui's first ScreenGui if not.
local screenGui = script.Parent
if not screenGui:IsA("ScreenGui") then
    screenGui = playerGui:FindFirstChildOfClass("ScreenGui")
end

-- Unity used auto-property getters that re-resolved each access; lazy
-- helpers preserve that in case the GUI is rebuilt at runtime.
local function module()
    return screenGui and screenGui:FindFirstChild("Module")
end
local function itemModule()
    return screenGui and screenGui:FindFirstChild("ItemModule")
end
local function ammo()
    local m = module()
    return m and m:FindFirstChild("Ammo")
end
local function pauseMenuObj()
    return screenGui and screenGui:FindFirstChild("Pause")
end
local function ammoCur()
    local a = ammo()
    return a and a:FindFirstChild("Cur")
end
local function health()
    local m = module()
    return m and m:FindFirstChild("Health")
end

-- Locate the Player LocalScript (sibling in PlayerScripts) to subscribe
-- to its BindableEvents. LocalScripts cannot be require()d, so this is
-- the only client-side IPC channel for the Player events.
local playerScripts = localPlayer:WaitForChild("PlayerScripts")
local function findPlayerScript()
    for _, c in playerScripts:GetDescendants() do
        if c:IsA("LocalScript") and c.Name == "Player" then
            return c
        end
    end
    return nil
end
local playerScript = findPlayerScript()
while not playerScript do
    playerScripts.DescendantAdded:Wait()
    playerScript = findPlayerScript()
end

local healthEvent = playerScript:WaitForChild("HealthUpdate")
local ammoEvent = playerScript:WaitForChild("AmmoUpdate")
local itemEvent = playerScript:WaitForChild("ItemUpdate")
local pauseEvent = playerScript:WaitForChild("PauseEvent")

-- Player.maxHealth / Player.maxAmmo are exposed as LocalPlayer attributes
-- by the Player script at init.
local maxHealth = localPlayer:GetAttribute("maxHealth") or 100
local maxAmmo = localPlayer:GetAttribute("maxAmmo") or 250

-- Awake equivalent: stamp the static "Total" ammo label.
local function initTotalAmmo()
    local a = ammo()
    if a then
        local total = a:FindFirstChild("Total")
        if total and (total:IsA("TextLabel") or total:IsA("TextButton")) then
            total.Text = tostring(maxAmmo)
        end
    end
end
initTotalAmmo()

local function pauseMenu(pause)
    -- AudioMixerSnapshot.TransitionTo has no Roblox equivalent and
    -- Time.timeScale is server-controlled in Roblox — pausing client-side
    -- physics is not possible. We only toggle the pause UI visibility;
    -- other systems (input, audio) are not muted.
    -- UNCONVERTED: AudioMixerSnapshot transitions (normal/muted)
    -- UNCONVERTED: Time.timeScale = 0 / 1
    local menu = pauseMenuObj()
    if menu and menu:IsA("GuiObject") then
        menu.Visible = pause
    end
end

local function updatePlayerItems(itemName)
    local im = itemModule()
    if not im then return end
    local item = im:FindFirstChild(itemName)
    if not item then return end
    -- Unity Toggle.isOn = true → mirror onto an "isOn" attribute and
    -- ensure the GUI is shown. Toggles in Unity reveal a checkmark; the
    -- exact visual treatment is left to whichever UIListener watches the
    -- attribute, but Visible=true covers the common pickup-icon case.
    item:SetAttribute("isOn", true)
    if item:IsA("GuiObject") then
        item.Visible = true
    end
end

local function updatePlayerAmmo(curAmmo)
    local ac = ammoCur()
    if ac and (ac:IsA("TextLabel") or ac:IsA("TextButton")) then
        ac.Text = tostring(curAmmo)
    end
end

local function updatePlayerHealth(curHealth)
    local h = health()
    if not h then return end
    local percentage = curHealth / maxHealth
    -- Unity Slider.value is 0..1. Roblox has no Slider class; mirror onto
    -- an attribute and resize a child Fill/Bar GuiObject if one exists.
    h:SetAttribute("value", percentage)
    local fill = h:FindFirstChild("Fill") or h:FindFirstChild("Bar")
    if fill and fill:IsA("GuiObject") then
        fill.Size = UDim2.new(percentage, 0, 1, 0)
    end
end

healthEvent.Event:Connect(updatePlayerHealth)
ammoEvent.Event:Connect(updatePlayerAmmo)
itemEvent.Event:Connect(updatePlayerItems)
pauseEvent.Event:Connect(pauseMenu)