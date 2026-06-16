-- Auto-generated animation script
-- Converted from Unity AnimationClip: KO
-- Duration: 2.00s, Loop: True
-- Inline TweenService per docs/design/inline-over-runtime-wrappers.md (no TransformAnimator / AnimatorBridge require).

local TweenService = game:GetService("TweenService")
local RunService = game:GetService("RunService")

-- No explicit target; search workspace for an object this animation might apply to
local target = nil
if not target then target = workspace:FindFirstChild("KOParticle", true) end
if not target then target = workspace:FindFirstChild("KOParticle (1)", true) end
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
	-- Rotation animation (20 keyframes)
	local basePos = target.Position
	local tweenInfo1 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween1 = TweenService:Create(target, tweenInfo1, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.75), math.rad(72.27), math.rad(-15.43))
	})
	tween1:Play()
	tween1.Completed:Wait()
	local tweenInfo2 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween2 = TweenService:Create(target, tweenInfo2, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.88), math.rad(51.27), math.rad(-15.29))
	})
	tween2:Play()
	tween2.Completed:Wait()
	local tweenInfo3 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween3 = TweenService:Create(target, tweenInfo3, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.91), math.rad(33.27), math.rad(-15.25))
	})
	tween3:Play()
	tween3.Completed:Wait()
	local tweenInfo4 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween4 = TweenService:Create(target, tweenInfo4, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.92), math.rad(15.27), math.rad(-15.22))
	})
	tween4:Play()
	tween4.Completed:Wait()
	local tweenInfo5 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween5 = TweenService:Create(target, tweenInfo5, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.92), math.rad(-5.72), math.rad(-15.19))
	})
	tween5:Play()
	tween5.Completed:Wait()
	local tweenInfo6 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween6 = TweenService:Create(target, tweenInfo6, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.92), math.rad(-23.72), math.rad(-15.17))
	})
	tween6:Play()
	tween6.Completed:Wait()
	local tweenInfo7 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween7 = TweenService:Create(target, tweenInfo7, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.90), math.rad(-41.72), math.rad(-15.13))
	})
	tween7:Play()
	tween7.Completed:Wait()
	local tweenInfo8 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween8 = TweenService:Create(target, tweenInfo8, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.84), math.rad(-62.72), math.rad(-15.05))
	})
	tween8:Play()
	tween8.Completed:Wait()
	local tweenInfo9 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween9 = TweenService:Create(target, tweenInfo9, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(89.54), math.rad(-80.72), math.rad(-14.74))
	})
	tween9:Play()
	tween9.Completed:Wait()
	local tweenInfo10 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween10 = TweenService:Create(target, tweenInfo10, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.51), math.rad(-81.27), math.rad(164.31))
	})
	tween10:Play()
	tween10.Completed:Wait()
	local tweenInfo11 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween11 = TweenService:Create(target, tweenInfo11, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.83), math.rad(-63.27), math.rad(164.65))
	})
	tween11:Play()
	tween11.Completed:Wait()
	local tweenInfo12 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween12 = TweenService:Create(target, tweenInfo12, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.90), math.rad(-42.28), math.rad(164.73))
	})
	tween12:Play()
	tween12.Completed:Wait()
	local tweenInfo13 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween13 = TweenService:Create(target, tweenInfo13, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.92), math.rad(-24.28), math.rad(164.77))
	})
	tween13:Play()
	tween13.Completed:Wait()
	local tweenInfo14 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween14 = TweenService:Create(target, tweenInfo14, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.92), math.rad(-6.28), math.rad(164.79))
	})
	tween14:Play()
	tween14.Completed:Wait()
	local tweenInfo15 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween15 = TweenService:Create(target, tweenInfo15, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.92), math.rad(14.72), math.rad(164.82))
	})
	tween15:Play()
	tween15.Completed:Wait()
	local tweenInfo16 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween16 = TweenService:Create(target, tweenInfo16, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.91), math.rad(32.72), math.rad(164.85))
	})
	tween16:Play()
	tween16.Completed:Wait()
	local tweenInfo17 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween17 = TweenService:Create(target, tweenInfo17, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.88), math.rad(50.72), math.rad(164.89))
	})
	tween17:Play()
	tween17.Completed:Wait()
	local tweenInfo18 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween18 = TweenService:Create(target, tweenInfo18, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-89.76), math.rad(71.72), math.rad(165.03))
	})
	tween18:Play()
	tween18.Completed:Wait()
	local tweenInfo19 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween19 = TweenService:Create(target, tweenInfo19, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-74.80), math.rad(89.71), math.rad(-180.00))
	})
	tween19:Play()
	tween19.Completed:Wait()
	-- Rotation animation (20 keyframes)
	local basePos = target.Position
	local tweenInfo1 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween1 = TweenService:Create(target, tweenInfo1, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(-72.00), math.rad(21.42))
	})
	tween1:Play()
	tween1.Completed:Wait()
	local tweenInfo2 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween2 = TweenService:Create(target, tweenInfo2, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(-51.00), math.rad(21.42))
	})
	tween2:Play()
	tween2.Completed:Wait()
	local tweenInfo3 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween3 = TweenService:Create(target, tweenInfo3, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(-33.00), math.rad(21.42))
	})
	tween3:Play()
	tween3.Completed:Wait()
	local tweenInfo4 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween4 = TweenService:Create(target, tweenInfo4, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(-15.00), math.rad(21.42))
	})
	tween4:Play()
	tween4.Completed:Wait()
	local tweenInfo5 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween5 = TweenService:Create(target, tweenInfo5, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(6.00), math.rad(21.42))
	})
	tween5:Play()
	tween5.Completed:Wait()
	local tweenInfo6 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween6 = TweenService:Create(target, tweenInfo6, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(24.00), math.rad(21.42))
	})
	tween6:Play()
	tween6.Completed:Wait()
	local tweenInfo7 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween7 = TweenService:Create(target, tweenInfo7, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(42.00), math.rad(21.42))
	})
	tween7:Play()
	tween7.Completed:Wait()
	local tweenInfo8 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween8 = TweenService:Create(target, tweenInfo8, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(63.00), math.rad(21.42))
	})
	tween8:Play()
	tween8.Completed:Wait()
	local tweenInfo9 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween9 = TweenService:Create(target, tweenInfo9, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(90.00), math.rad(81.00), math.rad(21.42))
	})
	tween9:Play()
	tween9.Completed:Wait()
	local tweenInfo10 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween10 = TweenService:Create(target, tweenInfo10, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(81.00), math.rad(-158.58))
	})
	tween10:Play()
	tween10.Completed:Wait()
	local tweenInfo11 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween11 = TweenService:Create(target, tweenInfo11, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(63.00), math.rad(-158.58))
	})
	tween11:Play()
	tween11.Completed:Wait()
	local tweenInfo12 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween12 = TweenService:Create(target, tweenInfo12, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(42.00), math.rad(-158.58))
	})
	tween12:Play()
	tween12.Completed:Wait()
	local tweenInfo13 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween13 = TweenService:Create(target, tweenInfo13, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(24.00), math.rad(-158.58))
	})
	tween13:Play()
	tween13.Completed:Wait()
	local tweenInfo14 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween14 = TweenService:Create(target, tweenInfo14, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(6.00), math.rad(-158.58))
	})
	tween14:Play()
	tween14.Completed:Wait()
	local tweenInfo15 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween15 = TweenService:Create(target, tweenInfo15, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(-15.00), math.rad(-158.58))
	})
	tween15:Play()
	tween15.Completed:Wait()
	local tweenInfo16 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween16 = TweenService:Create(target, tweenInfo16, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(-33.00), math.rad(-158.58))
	})
	tween16:Play()
	tween16.Completed:Wait()
	local tweenInfo17 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween17 = TweenService:Create(target, tweenInfo17, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(-51.00), math.rad(-158.58))
	})
	tween17:Play()
	tween17.Completed:Wait()
	local tweenInfo18 = TweenInfo.new(0.1167, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween18 = TweenService:Create(target, tweenInfo18, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(-90.00), math.rad(-72.00), math.rad(-158.58))
	})
	tween18:Play()
	tween18.Completed:Wait()
	local tweenInfo19 = TweenInfo.new(0.1000, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)
	local tween19 = TweenService:Create(target, tweenInfo19, {
		CFrame = CFrame.new(basePos) * CFrame.Angles(math.rad(94.83), math.rad(-89.98), math.rad(6.67))
	})
	tween19:Play()
	tween19.Completed:Wait()
end

-- Loop the animation
while true do
	playAnimation()
end
