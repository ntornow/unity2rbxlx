-- Auto-generated animation script
-- Converted from Unity AnimationClip: open
-- Duration: 1.00s, Loop: False
-- Inline TweenService per docs/design/inline-over-runtime-wrappers.md (no TransformAnimator / AnimatorBridge require).

local TweenService = game:GetService("TweenService")
local RunService = game:GetService("RunService")

-- Prefab-scoped: prefer script.Parent (this clone), fall
-- back to workspace search when running from a global container.
local target
if script.Parent and (script.Parent:IsA('Model') or script.Parent:IsA('BasePart')) then
    target = script.Parent:FindFirstChild("door", true) or script.Parent
else
    target = workspace:FindFirstChild("door", true)
end
if not target then
    -- Animation target not found; script will not run
    return
end

-- If target is a Model, use its PrimaryPart or first BasePart
if target:IsA('Model') then
    target = target.PrimaryPart or target:FindFirstChildWhichIsA('BasePart') or target
end

local _initialTarget = target
local function playAnimation(target)
	target = target or _initialTarget
	-- Position animation (2 keyframes)
	local basePos = target.Position
	local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween1 = TweenService:Create(target, tweenInfo1, {
		Position = basePos + Vector3.new(0.0000, 4.0000, -0.0000)
	})
	tween1:Play()
	tween1.Completed:Wait()
end

-- Parameter-driven animation: open (bool)
local _targets = { target }
local _ownerIsContainer = script.Parent and (script.Parent:IsA('BasePart') or script.Parent:IsA('Model'))
if not _ownerIsContainer then
	local _targetName = target.Name
	for _, _t in ipairs(workspace:GetDescendants()) do
		if _t ~= target and _t.Name == _targetName and (_t:IsA('BasePart') or _t:IsA('Model')) then
			if _t:IsA('Model') then _t = _t.PrimaryPart or _t:FindFirstChildWhichIsA('BasePart') or _t end
			if _t and _t:IsA('BasePart') then table.insert(_targets, _t) end
		end
	end
end

for _, _t in ipairs(_targets) do
	local _isActive = false
	_t:SetAttribute("open", false)
	_t:GetAttributeChangedSignal("open"):Connect(function()
		local val = _t:GetAttribute("open")
		if val and not _isActive then
			_isActive = true
			playAnimation(_t)
			_isActive = false
		end
	end)
end
