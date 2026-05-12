-- hit_detection.luau: HitDetection-family capability handlers.
--
-- Owns the ``ctx.hitDetection`` namespace. Publishes the impact CFrame
-- + hit Instance for downstream Effect capabilities to read.
--
-- Cross-family signal: ``ctx.hitDetection.impactSignal`` is a
-- BindableEvent fired on each impact. Effect capabilities subscribe
-- to it rather than polling ``ctx.hitDetection.lastInstance`` so the
-- effects only fire on transitions, not on every Heartbeat.
--
-- PR #73b ships RaycastSegment + OverlapSphere. ``Touched`` is
-- intentionally NOT a separate capability — the legacy bullet pack's
-- 12-round saga of fixes around Touched tunneling at high speeds
-- makes RaycastSegment the safe default.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local Composer = require(script.Parent:WaitForChild("Composer"))

local HitDetection = {}

local function _rootPartOf(container: Instance): BasePart?
    if container:IsA("BasePart") then return container end
    if container:IsA("Model") then
        return container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    end
    return nil
end

local function _modelOf(inst: Instance): Model?
    if inst:IsA("Model") then return inst end
    return inst:FindFirstAncestorOfClass("Model")
end

-- Filter out the bullet's own descendants so a fast bullet doesn't
-- self-hit on the first Heartbeat (the trail/anti-grav attachments
-- count as descendants).
local function _excludeFilter(container: Instance): RaycastParams
    local rp = RaycastParams.new()
    rp.FilterDescendantsInstances = { container }
    rp.FilterType = Enum.RaycastFilterType.Exclude
    return rp
end

-- ``HitDetection.RaycastSegment`` semantics:
--
--   1. Each ``Heartbeat``, segment-cast from the previous-frame
--      position to the current. Catches tunneling that Touched would
--      miss (a ``force=200`` bullet covers ~12 studs per frame).
--   2. On hit, set ``ctx.hitDetection.lastInstance`` /
--      ``lastImpactCFrame`` and fire ``impactSignal``. Effect
--      capabilities subscribe to the signal.
--   3. After firing, set ``consumed = true`` so a single bullet
--      doesn't fire multiple hits across consecutive frames at the
--      same wall. The Effect family is expected to despawn the
--      container itself; this handler doesn't.
function HitDetection.raycastSegment(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local rootPart = _rootPartOf(container)
    if rootPart == nil then
        warn(string.format(
            "[gameplay/hitDetection] RaycastSegment: no BasePart on %s",
            container:GetFullName()
        ))
        return
    end

    local impactSignal = Instance.new("BindableEvent")
    ctx.hitDetection.impactSignal = impactSignal
    -- Initialize so Effect handlers don't see ``nil`` at bind time
    -- (the signal is the trigger; the values are read on signal fire).
    ctx.hitDetection.lastInstance = nil
    ctx.hitDetection.lastImpactCFrame = nil

    local rp = _excludeFilter(container)
    local prevPos = rootPart.Position
    local consumed = false
    local conn: RBXScriptConnection? = nil
    conn = RunService.Heartbeat:Connect(function()
        if consumed or rootPart.Parent == nil then
            if conn then
                conn:Disconnect()
                conn = nil
            end
            return
        end
        local curPos = rootPart.Position
        local segment = curPos - prevPos
        if segment.Magnitude > 0 then
            local result = workspace:Raycast(prevPos, segment, rp)
            if result then
                consumed = true
                ctx.hitDetection.lastInstance = result.Instance
                ctx.hitDetection.lastImpactCFrame = CFrame.new(result.Position)
                impactSignal:Fire(result.Instance, result.Position)
            end
        end
        prevPos = curPos
    end)
end

-- ``HitDetection.OverlapSphere``: one-shot sphere cast at the
-- container's position. Fires ``impactSignal`` once per matched
-- instance. Used for proximity sensors / trigger volumes — not for
-- bullets (which use RaycastSegment for tunneling-safety).
function HitDetection.overlapSphere(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local rootPart = _rootPartOf(container)
    if rootPart == nil then
        warn(string.format(
            "[gameplay/hitDetection] OverlapSphere: no BasePart on %s",
            container:GetFullName()
        ))
        return
    end
    local impactSignal = Instance.new("BindableEvent")
    ctx.hitDetection.impactSignal = impactSignal
    ctx.hitDetection.lastInstance = nil
    ctx.hitDetection.lastImpactCFrame = nil

    local radiusUnity = cap.radius_unity :: number
    -- Convert to stud-space — Unity OverlapSphere works in meters.
    local radiusStuds = radiusUnity * 3.571

    local op = OverlapParams.new()
    op.FilterDescendantsInstances = { container }
    op.FilterType = Enum.RaycastFilterType.Exclude
    local parts = workspace:GetPartBoundsInRadius(rootPart.Position, radiusStuds, op)
    for _, part in ipairs(parts) do
        ctx.hitDetection.lastInstance = part
        ctx.hitDetection.lastImpactCFrame = CFrame.new(part.Position)
        impactSignal:Fire(part, part.Position)
    end
end

-- Module-scope export so other gameplay modules (effects.luau)
-- can reuse the model/Player lookup without re-implementing it.
function HitDetection._modelOf(inst: Instance): Model?
    return _modelOf(inst)
end

Composer.register("hit_detection.raycast_segment", HitDetection.raycastSegment)
Composer.register("hit_detection.overlap_sphere", HitDetection.overlapSphere)

return HitDetection
