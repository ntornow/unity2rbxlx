-- u2r: disable default PlayerModule controls + assert FPS mouse state + first-person body hide + spawn floor-snap
-- Re-applies on CharacterAdded because Roblox's character spawn flow
-- re-enables the default PlayerModule and resets MouseBehavior, and
-- because Roblox loads avatar accessories asynchronously after the
-- character spawns — DescendantAdded catches each late-added Handle
-- so the user's hat/chain/glasses don't float across the FPS camera.
do
    local _lp = game:GetService("Players").LocalPlayer
    local _UIS = game:GetService("UserInputService")
    local function _applyFpsMouseState()
        local _ps = _lp:WaitForChild("PlayerScripts", 10)
        local _pm = _ps and _ps:WaitForChild("PlayerModule", 10)
        if _pm then
            local ok, mod = pcall(require, _pm)
            if ok and mod then
                local ok2, controls = pcall(function() return mod:GetControls() end)
                if ok2 and controls and controls.Disable then
                    pcall(function() controls:Disable() end)
                end
            end
        end
        _UIS.MouseBehavior = Enum.MouseBehavior.LockCenter
        _UIS.MouseIconEnabled = false
    end
    local function _isInWeaponSlot(inst)
        local p = inst.Parent
        while p and p ~= game do
            if p.Name == "WeaponSlot" then return true end
            p = p.Parent
        end
        return false
    end
    local function _hidePart(part)
        if (part:IsA("BasePart") or part:IsA("Decal")) and not _isInWeaponSlot(part) then
            part.LocalTransparencyModifier = 1
        end
    end
    local function _hideCharacter(char)
        if not char then return end
        char.DescendantAdded:Connect(_hidePart)
        for _, part in char:GetDescendants() do _hidePart(part) end
    end
    local function _snapToFloor(char)
        if not char then return end
        local hrp = char:WaitForChild("HumanoidRootPart", 5)
        if not hrp then return end
        task.wait()
        local rp = RaycastParams.new()
        rp.FilterDescendantsInstances = {char}
        rp.FilterType = Enum.RaycastFilterType.Exclude
        local hit = workspace:Raycast(hrp.Position, Vector3.new(0, -200, 0), rp)
        if not hit then return end
        local target = hit.Position + Vector3.new(0, 3, 0)
        if (hrp.Position - target).Magnitude > 2 then
            hrp.CFrame = hrp.CFrame + (target - hrp.Position)
        end
    end
    _applyFpsMouseState()
    _hideCharacter(_lp.Character)
    _snapToFloor(_lp.Character)
    _lp.CharacterAdded:Connect(function(char)
        task.wait()
        _applyFpsMouseState()
        _hideCharacter(char)
        _snapToFloor(char)
    end)
end

local UserInputService = game:GetService("UserInputService")
local Players = game:GetService("Players")

local player = Players.LocalPlayer

local function awake()
    UserInputService.MouseIconEnabled = false
    UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
end

local function restartTimer(time)
    task.wait(time)
    player:LoadCharacter()
end

local function restartGame(time)
    task.spawn(restartTimer, time)
end

awake()