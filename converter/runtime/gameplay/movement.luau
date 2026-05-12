-- movement.luau: Movement-family capability handlers.
--
-- Owns the ``ctx.movement`` namespace.
--
-- PR #73a ships ``Movement.AttributeDrivenTween`` only. The Impulse /
-- ConstantVelocity capabilities arrive in PR #73b alongside the
-- projectile slice.

local TweenService = game:GetService("TweenService")
local Composer = require(script.Parent:WaitForChild("Composer"))

local Movement = {}

-- 1 Unity meter ≈ 3.571 Roblox studs. Mirrors the constant baked into
-- ``converter/CLAUDE.md`` and the legacy bullet/door coherence packs.
local _STUDS_PER_METER: number = 3.571

-- ``Movement.AttributeDrivenTween`` semantics:
--
--   1. On first bind, read ``ctx.trigger.value`` and SNAP to the
--      open or closed pose (no animation). A door spawned with
--      ``open=true`` reads as already-open.
--   2. On ``ctx.trigger.value`` change, cancel any in-flight tween
--      and start a new one from the part's CURRENT CFrame toward
--      the appropriate target. No snapping mid-motion — preserves
--      rapid-toggle correctness.
--   3. Idempotent re-bind is handled at the Composer level via the
--      ``_GameplayBound`` marker; we don't second-guess.
--
-- We need to observe ``ctx.trigger.value`` changes. The trigger
-- handler updates the field but doesn't fire a signal. We connect
-- to the underlying container attribute's ``GetAttributeChangedSignal``
-- the trigger handler already binds to. Rather than introducing a
-- cross-family signal, we register a SECONDARY attribute listener
-- here on the same attribute. The trigger handler owns ctx writes;
-- the movement handler owns visual mutation. Both observe the same
-- raw attribute, so they converge.
--
-- This requires the movement capability to know WHICH attribute the
-- trigger watches. In PR #73a's door slice, the only trigger-driven
-- mover is the door, so we look up the value once at bind time, then
-- re-read it via ``ctx.trigger.value`` on every attribute-change
-- callback. The container we register on is the container Composer
-- passed in — same one Trigger.OnBoolAttribute bound to — so the
-- two stay in sync.
function Movement.attributeDrivenTween(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    if not container:IsA("BasePart") then
        warn(string.format(
            "[gameplay/movement] AttributeDrivenTween needs a BasePart "
            .. "container, got %s (%s)",
            container.ClassName, container:GetFullName()
        ))
        return
    end
    local targetOffsetUnity = cap.target_offset_unity :: Vector3
    local openDuration = cap.open_duration :: number
    local closeDuration = cap.close_duration :: number

    local offsetStuds = targetOffsetUnity * _STUDS_PER_METER
    local closedCFrame = container.CFrame
    local openCFrame = closedCFrame + offsetStuds

    -- Initial pose: snap based on the trigger value the
    -- Trigger.OnBoolAttribute handler already published. If the
    -- trigger handler ran first (validator enforces this order),
    -- ctx.trigger.value is a strict bool here.
    if ctx.trigger.value then
        container.CFrame = openCFrame
    else
        container.CFrame = closedCFrame
    end

    local currentTween: Tween? = nil
    local function _animateTo(target: CFrame, duration: number): ()
        if currentTween then
            currentTween:Cancel()
        end
        currentTween = TweenService:Create(
            container,
            TweenInfo.new(duration, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
            { CFrame = target }
        )
        currentTween:Play()
    end

    -- React to trigger changes via the BindableEvent the trigger
    -- handler published in ctx.trigger.changed. That stays on the
    -- ctx surface; movement never reaches back to the underlying
    -- Roblox attribute, so the trigger family stays free to back
    -- ctx.trigger.value with something other than an attribute in
    -- future variants.
    local changed = ctx.trigger.changed :: BindableEvent?
    if changed == nil then
        warn("[gameplay/movement] AttributeDrivenTween needs "
            .. "ctx.trigger.changed (no trigger family ran first)")
        return
    end
    changed.Event:Connect(function(value: boolean)
        if value then
            _animateTo(openCFrame, openDuration)
        else
            _animateTo(closedCFrame, closeDuration)
        end
    end)
end

Composer.register("movement.attribute_driven_tween", Movement.attributeDrivenTween)

return Movement
