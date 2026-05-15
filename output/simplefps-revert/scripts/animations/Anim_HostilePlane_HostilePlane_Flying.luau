-- Auto-generated animation script
-- Converted from Unity AnimationClip: Flying
-- Duration: 16.00s, Loop: True
-- Inline TweenService per docs/design/inline-over-runtime-wrappers.md (no TransformAnimator / AnimatorBridge require).

local TweenService = game:GetService("TweenService")
local RunService = game:GetService("RunService")

-- Prefab-scoped: prefer script.Parent (this clone), fall
-- back to workspace search when running from a global container.
local target
if script.Parent and (script.Parent:IsA('Model') or script.Parent:IsA('BasePart')) then
    target = script.Parent:FindFirstChild("HostilePlane", true) or script.Parent
else
    target = workspace:FindFirstChild("HostilePlane", true)
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
	-- Position animation (5 keyframes)
	local basePos = target.Position
	local tweenInfo1 = TweenInfo.new(4.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween1 = TweenService:Create(target, tweenInfo1, {
		Position = basePos + Vector3.new(375.0000, 200.0000, -0.0000)
	})
	tween1:Play()
	tween1.Completed:Wait()
	local tweenInfo2 = TweenInfo.new(4.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween2 = TweenService:Create(target, tweenInfo2, {
		Position = basePos + Vector3.new(600.0000, 200.0000, -300.0000)
	})
	tween2:Play()
	tween2.Completed:Wait()
	local tweenInfo3 = TweenInfo.new(4.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween3 = TweenService:Create(target, tweenInfo3, {
		Position = basePos + Vector3.new(375.0000, 200.0000, -600.0000)
	})
	tween3:Play()
	tween3.Completed:Wait()
	local tweenInfo4 = TweenInfo.new(4.0000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween4 = TweenService:Create(target, tweenInfo4, {
		Position = basePos + Vector3.new(0.0000, 200.0000, -300.0000)
	})
	tween4:Play()
	tween4.Completed:Wait()
	-- Rotation animation (20 keyframes)
	local basePos = target.Position
	local tweenInfo1 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween1 = TweenService:Create(target, tweenInfo1, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(83.24), math.rad(17.93), math.rad(-21.07))
	})
	tween1:Play()
	tween1.Completed:Wait()
	local tweenInfo2 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween2 = TweenService:Create(target, tweenInfo2, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(75.10), math.rad(35.23), math.rad(-24.75))
	})
	tween2:Play()
	tween2.Completed:Wait()
	local tweenInfo3 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween3 = TweenService:Create(target, tweenInfo3, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(62.23), math.rad(52.01), math.rad(-33.75))
	})
	tween3:Play()
	tween3.Completed:Wait()
	local tweenInfo4 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween4 = TweenService:Create(target, tweenInfo4, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(36.60), math.rad(65.61), math.rad(-55.93))
	})
	tween4:Play()
	tween4.Completed:Wait()
	local tweenInfo5 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween5 = TweenService:Create(target, tweenInfo5, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-14.00), math.rad(69.44), math.rad(-103.14))
	})
	tween5:Play()
	tween5.Completed:Wait()
	local tweenInfo6 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween6 = TweenService:Create(target, tweenInfo6, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-51.98), math.rad(59.42), math.rad(-137.75))
	})
	tween6:Play()
	tween6.Completed:Wait()
	local tweenInfo7 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween7 = TweenService:Create(target, tweenInfo7, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-69.70), math.rad(43.63), math.rad(-151.80))
	})
	tween7:Play()
	tween7.Completed:Wait()
	local tweenInfo8 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween8 = TweenService:Create(target, tweenInfo8, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-79.48), math.rad(26.64), math.rad(-157.50))
	})
	tween8:Play()
	tween8.Completed:Wait()
	local tweenInfo9 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween9 = TweenService:Create(target, tweenInfo9, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-86.77), math.rad(8.80), math.rad(-159.75))
	})
	tween9:Play()
	tween9.Completed:Wait()
	local tweenInfo10 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween10 = TweenService:Create(target, tweenInfo10, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-93.23), math.rad(-8.81), math.rad(-159.75))
	})
	tween10:Play()
	tween10.Completed:Wait()
	local tweenInfo11 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween11 = TweenService:Create(target, tweenInfo11, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-100.52), math.rad(-26.64), math.rad(-157.50))
	})
	tween11:Play()
	tween11.Completed:Wait()
	local tweenInfo12 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween12 = TweenService:Create(target, tweenInfo12, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-110.30), math.rad(-43.63), math.rad(-151.80))
	})
	tween12:Play()
	tween12.Completed:Wait()
	local tweenInfo13 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween13 = TweenService:Create(target, tweenInfo13, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-128.02), math.rad(-59.42), math.rad(-137.75))
	})
	tween13:Play()
	tween13.Completed:Wait()
	local tweenInfo14 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween14 = TweenService:Create(target, tweenInfo14, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-166.00), math.rad(-69.44), math.rad(-103.14))
	})
	tween14:Play()
	tween14.Completed:Wait()
	local tweenInfo15 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween15 = TweenService:Create(target, tweenInfo15, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(143.40), math.rad(-65.61), math.rad(-55.93))
	})
	tween15:Play()
	tween15.Completed:Wait()
	local tweenInfo16 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween16 = TweenService:Create(target, tweenInfo16, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(117.77), math.rad(-52.01), math.rad(-33.75))
	})
	tween16:Play()
	tween16.Completed:Wait()
	local tweenInfo17 = TweenInfo.new(0.8500, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween17 = TweenService:Create(target, tweenInfo17, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(104.90), math.rad(-35.23), math.rad(-24.75))
	})
	tween17:Play()
	tween17.Completed:Wait()
	local tweenInfo18 = TweenInfo.new(0.8333, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween18 = TweenService:Create(target, tweenInfo18, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(96.76), math.rad(-17.93), math.rad(-21.07))
	})
	tween18:Play()
	tween18.Completed:Wait()
	local tweenInfo19 = TweenInfo.new(0.8501, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween19 = TweenService:Create(target, tweenInfo19, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(0.00), math.rad(-20.00))
	})
	tween19:Play()
	tween19.Completed:Wait()
end

-- Loop the animation
while true do
	playAnimation()
end
