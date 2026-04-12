-- UnityBridge/Input
-- Maps Unity's Input system to Roblox UserInputService.
-- Usage:
--   local Input = require(ReplicatedStorage.UnityBridge.Input)
--   if Input.GetKeyDown("Space") then ... end
--   local h = Input.GetAxis("Horizontal") -- -1, 0, or 1

local UserInputService = game:GetService("UserInputService")

local Input = {}

-- Current frame key state
local keysDown = {}
local keysPressed = {}  -- just this frame
local keysReleased = {} -- just this frame
local axisValues = { Horizontal = 0, Vertical = 0 }

-- Unity key name → Roblox KeyCode
local KEY_MAP = {
	Space = Enum.KeyCode.Space,
	W = Enum.KeyCode.W,
	A = Enum.KeyCode.A,
	S = Enum.KeyCode.S,
	D = Enum.KeyCode.D,
	LeftArrow = Enum.KeyCode.Left,
	RightArrow = Enum.KeyCode.Right,
	UpArrow = Enum.KeyCode.Up,
	DownArrow = Enum.KeyCode.Down,
	LeftShift = Enum.KeyCode.LeftShift,
	RightShift = Enum.KeyCode.RightShift,
	Return = Enum.KeyCode.Return,
	Escape = Enum.KeyCode.Escape,
}

-- Track key state
UserInputService.InputBegan:Connect(function(input, processed)
	if processed then return end
	if input.UserInputType == Enum.UserInputType.Keyboard then
		keysDown[input.KeyCode] = true
		keysPressed[input.KeyCode] = true
	end
end)

UserInputService.InputEnded:Connect(function(input)
	if input.UserInputType == Enum.UserInputType.Keyboard then
		keysDown[input.KeyCode] = nil
		keysReleased[input.KeyCode] = true
	end
end)

-- Touch/swipe tracking
local touchStart = nil
local lastSwipe = nil

UserInputService.TouchStarted:Connect(function(touch)
	touchStart = touch.Position
	lastSwipe = nil
end)

UserInputService.TouchEnded:Connect(function(touch)
	if not touchStart then return end
	local delta = touch.Position - touchStart
	if math.abs(delta.X) > math.abs(delta.Y) then
		if delta.X > 40 then lastSwipe = "Right"
		elseif delta.X < -40 then lastSwipe = "Left" end
	else
		if delta.Y < -40 then lastSwipe = "Up"
		elseif delta.Y > 40 then lastSwipe = "Down" end
	end
	touchStart = nil
end)

function Input.GetKey(name)
	local keyCode = KEY_MAP[name]
	if keyCode then return keysDown[keyCode] == true end
	return false
end

function Input.GetKeyDown(name)
	local keyCode = KEY_MAP[name]
	if keyCode then return keysPressed[keyCode] == true end
	return false
end

function Input.GetKeyUp(name)
	local keyCode = KEY_MAP[name]
	if keyCode then return keysReleased[keyCode] == true end
	return false
end

function Input.GetAxis(axisName)
	if axisName == "Horizontal" then
		local v = 0
		if keysDown[Enum.KeyCode.A] or keysDown[Enum.KeyCode.Left] then v = v - 1 end
		if keysDown[Enum.KeyCode.D] or keysDown[Enum.KeyCode.Right] then v = v + 1 end
		return v
	elseif axisName == "Vertical" then
		local v = 0
		if keysDown[Enum.KeyCode.S] or keysDown[Enum.KeyCode.Down] then v = v - 1 end
		if keysDown[Enum.KeyCode.W] or keysDown[Enum.KeyCode.Up] then v = v + 1 end
		return v
	end
	return 0
end

function Input.GetSwipe()
	local s = lastSwipe
	lastSwipe = nil
	return s
end

-- Call at end of each frame to clear per-frame state
function Input._EndFrame()
	keysPressed = {}
	keysReleased = {}
end

return Input
