-- Auto-generated animation script
-- Converted from Unity AnimationClip: holder2
-- Duration: 1.00s, Loop: False
-- Inline TweenService per docs/design/inline-over-runtime-wrappers.md (no TransformAnimator / AnimatorBridge require).

local TweenService = game:GetService("TweenService")
local RunService = game:GetService("RunService")

-- Prefab-scoped: prefer script.Parent (this clone), fall
-- back to workspace search when running from a global container.
local target
if script.Parent and (script.Parent:IsA('Model') or script.Parent:IsA('BasePart')) then
    target = script.Parent:FindFirstChild("PlaneHolder", true) or script.Parent
else
    target = workspace:FindFirstChild("PlaneHolder", true)
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
	local Base11_Box2 = target:FindFirstChild("Box2")
	if Base11_Box2 then
		-- Position animation (2 keyframes)
		local basePos = Base11_Box2.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base11_Box2, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base10_Box1 = target:FindFirstChild("Box1")
	if Base10_Box1 then
		-- Position animation (2 keyframes)
		local basePos = Base10_Box1.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base10_Box1, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base6_Box2 = target:FindFirstChild("Box2")
	if Base6_Box2 then
		-- Position animation (2 keyframes)
		local basePos = Base6_Box2.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base6_Box2, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base7_Box1 = target:FindFirstChild("Box1")
	if Base7_Box1 then
		-- Position animation (2 keyframes)
		local basePos = Base7_Box1.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base7_Box1, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base8_Box2 = target:FindFirstChild("Box2")
	if Base8_Box2 then
		-- Position animation (2 keyframes)
		local basePos = Base8_Box2.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base8_Box2, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base9_Box1 = target:FindFirstChild("Box1")
	if Base9_Box1 then
		-- Position animation (2 keyframes)
		local basePos = Base9_Box1.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base9_Box1, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base4_Box2 = target:FindFirstChild("Box2")
	if Base4_Box2 then
		-- Position animation (2 keyframes)
		local basePos = Base4_Box2.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base4_Box2, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
	local Base5_Box1 = target:FindFirstChild("Box1")
	if Base5_Box1 then
		-- Position animation (2 keyframes)
		local basePos = Base5_Box1.Position
		local tweenInfo1 = TweenInfo.new(1.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
		local tween1 = TweenService:Create(Base5_Box1, tweenInfo1, {
			Position = basePos + Vector3.new(0.0000, 0.0000, 2.0000)
		})
		tween1:Play()
		tween1.Completed:Wait()
	end
end

-- Parameter-driven animation: actionNumber (int)
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
	_t:SetAttribute("actionNumber", 0)
	_t:GetAttributeChangedSignal("actionNumber"):Connect(function()
		playAnimation(_t)
	end)
end
