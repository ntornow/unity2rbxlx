-- movement.luau: Movement-family capability handlers.
--
-- Owns the ``ctx.movement`` namespace.
--
-- Ships ``Movement.AttributeDrivenTween`` (PR #73a, door slice) and
-- ``Movement.Impulse`` (PR #73b, projectile slice).

local TweenService = game:GetService("TweenService")
local Composer = require(script.Parent:WaitForChild("Composer"))

local Movement = {}

-- 1 Unity meter ≈ 3.571 Roblox studs. Mirrors the constant baked into
-- ``converter/CLAUDE.md`` and the legacy bullet/door coherence packs.
local _STUDS_PER_METER: number = 3.571

-- Locate the part the impulse should act on. Bullets are sometimes
-- bound as ``Model`` containers (prefab with mesh children) and
-- sometimes as a single ``BasePart``. Returns nil if the container
-- has no usable physics body.
local function _rootPartOf(container: Instance): BasePart?
    if container:IsA("BasePart") then
        return container
    end
    if container:IsA("Model") then
        if container.PrimaryPart then
            return container.PrimaryPart
        end
        return container:FindFirstChildWhichIsA("BasePart")
    end
    return nil
end

-- Container CFrame source — Model:GetPivot() for prefab containers,
-- raw CFrame for parts. Used so the impulse direction respects the
-- container's orientation regardless of whether it's a part or model.
local function _containerCFrame(container: Instance, rootPart: BasePart): CFrame
    if container:IsA("Model") then
        return container:GetPivot()
    end
    return rootPart.CFrame
end

-- ``Movement.Impulse`` semantics:
--
--   1. Read ``cap.force_unity`` (Unity m/s) — but allow a runtime
--      attribute override on the container. SimpleFPS prefabs serialize
--      ``force`` as an attribute via the MonoBehaviour extractor; the
--      attribute takes precedence so per-instance Unity inspector
--      tuning survives the conversion. Same pattern for ``fadeTime``
--      / ``damage`` in their respective capabilities.
--   2. Multiply by STUDS_PER_METER to get stud-space velocity.
--      Roblox impulses are in stud-units; ``200`` Unity m/s would be
--      ~57 studs/s without the conversion (5.7x too slow).
--   3. Install an anti-gravity VectorForce so the bullet flies
--      straight like Unity's near-massless impulse rather than diving
--      under Roblox's 196 studs/s² gravity (5.6x stronger than
--      Unity's 9.81 m/s²).
--   4. Write the velocity vector to ``ctx.movement.velocity`` so the
--      HitDetection.RaycastSegment handler (validator-enforced to run
--      after this one) has a stable initial direction.
function Movement.impulse(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local rootPart = _rootPartOf(container)
    if rootPart == nil then
        warn(string.format(
            "[gameplay/movement] Impulse: no BasePart on %s",
            container:GetFullName()
        ))
        return
    end
    local directionLocal = cap.direction_local :: Vector3
    -- Per-instance attribute override. The legacy bullet pack read
    -- ``_attrHost:GetAttribute("force")`` for the same reason — Unity
    -- prefab inspector overrides flow through ``_extract_monobehaviour_attributes``
    -- onto the part as a numeric attribute. Falling back to the
    -- capability's emit-time default preserves the Unity-canonical
    -- value when the attribute is absent.
    local attrHost: Instance = rootPart
    local overrideForce = attrHost:GetAttribute("force")
    local forceUnity: number = (typeof(overrideForce) == "number" and overrideForce)
        or (cap.force_unity :: number)

    rootPart.Anchored = false
    local cframe = _containerCFrame(container, rootPart)
    local worldDir = cframe:VectorToWorldSpace(directionLocal)
    if worldDir.Magnitude == 0 then
        warn(string.format(
            "[gameplay/movement] Impulse: zero direction on %s — no velocity applied",
            container:GetFullName()
        ))
        return
    end
    local studsVelocity = worldDir.Unit * (forceUnity * _STUDS_PER_METER)
    rootPart.AssemblyLinearVelocity = studsVelocity

    -- Anti-gravity VectorForce. Named ``_GameplayAntiGravity`` so a
    -- re-bind path (Studio reload, attribute change re-emit) can find
    -- and skip rather than double-install. The composer's _GameplayBound
    -- marker on the CONTAINER catches the common case; this rootPart-
    -- level guard handles model-container shapes where the marker
    -- lives on a non-physics ancestor.
    local existing = rootPart:FindFirstChild("_GameplayAntiGravity")
    if existing == nil then
        local att = Instance.new("Attachment")
        att.Name = "_GameplayAntiGravityAtt"
        att.Parent = rootPart
        local antiG = Instance.new("VectorForce")
        antiG.Name = "_GameplayAntiGravity"
        antiG.Force = Vector3.new(0, workspace.Gravity * rootPart.AssemblyMass, 0)
        antiG.RelativeTo = Enum.ActuatorRelativeTo.World
        antiG.Attachment0 = att
        antiG.ApplyAtCenterOfMass = true
        antiG.Parent = rootPart
    end

    ctx.movement.velocity = studsVelocity
end

Composer.register("movement.impulse", Movement.impulse)

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
