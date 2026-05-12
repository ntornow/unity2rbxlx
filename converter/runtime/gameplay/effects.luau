-- effects.luau: Effect-family capability handlers.
--
-- Owns the ``ctx.effect`` namespace. Subscribes to
-- ``ctx.hitDetection.impactSignal`` for hit-driven effects.
--
-- All effects despawn the container on first fire to match the legacy
-- bullet pack semantics — Unity ``OnCollisionEnter`` calls
-- ``Destroy(gameObject)`` so the container should not survive its
-- own effect. ``Lifetime.Despawn`` runs in parallel and handles the
-- never-hit case.

local Debris = game:GetService("Debris")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Composer = require(script.Parent:WaitForChild("Composer"))

local Effects = {}

local _STUDS_PER_METER: number = 3.571

local function _modelOf(inst: Instance): Model?
    if inst:IsA("Model") then return inst end
    return inst:FindFirstAncestorOfClass("Model")
end

-- Resolve the per-instance damage value, honoring an inspector
-- override via the ``damage`` attribute on the container (legacy
-- bullet pack pattern — Unity prefab inspector overrides flow
-- through MonoBehaviour attribute extraction).
local function _resolveValue(container: Instance, capValue: number, attrName: string): number
    local attrHost: Instance = container
    if container:IsA("Model") then
        attrHost = container.PrimaryPart or container
    end
    local override = attrHost:GetAttribute(attrName)
    if typeof(override) == "number" then
        return override
    end
    return capValue
end

local function _applyDamageToModel(model: Model, value: number): ()
    -- Mirror the legacy pack: set a ``TakeDamage`` attribute on the
    -- model so a server-side damage router (PR #73c) can observe
    -- client-fired hits without the LocalScript→server replication
    -- gap that bit PR #71. Also call Humanoid:TakeDamage directly so
    -- the conversion works pre-#73c.
    model:SetAttribute("TakeDamage", value)
    local humanoid = model:FindFirstChildOfClass("Humanoid")
    if humanoid then
        humanoid:TakeDamage(value)
    end
end

-- ``Effect.Damage`` semantics:
--
--   On each impact, find the Humanoid-bearing ancestor of the hit
--   Instance and apply ``value`` damage. No-op if the hit isn't a
--   character (terrain, wall, prop). The container is destroyed
--   after damage is applied so the bullet doesn't continue past the
--   hit.
function Effects.damage(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local signal = ctx.hitDetection.impactSignal :: BindableEvent?
    if signal == nil then
        warn(string.format(
            "[gameplay/effects] Damage needs ctx.hitDetection.impactSignal "
            .. "(no HitDetection family ran first) on %s",
            container:GetFullName()
        ))
        return
    end
    local capValue = cap.value :: number
    signal.Event:Connect(function(hitInstance: Instance, _impactPos: Vector3)
        local model = _modelOf(hitInstance)
        if model == nil then
            -- Non-character hit (terrain, wall, prop). Direct-hit
            -- bullets despawn without damage — matches Unity
            -- TurretBullet.cs OnCollisionEnter's non-Player branch.
            if container.Parent then container:Destroy() end
            return
        end
        if model:FindFirstChildOfClass("Humanoid") == nil then
            if container.Parent then container:Destroy() end
            return
        end
        local value = _resolveValue(container, capValue, "damage")
        _applyDamageToModel(model, value)
        if container.Parent then container:Destroy() end
    end)
end

-- ``Effect.Splash`` semantics:
--
--   On impact, sphere-cast at the impact point and damage every
--   Player within ``radius_unity`` (Unity meters → studs). Centers
--   on the IMPACT CFrame, NOT the bullet root — codex PR #71-round-5
--   P1 flagged that tunneling speeds put the root meters past the
--   collision point, and a 7-stud splash radius would miss a player
--   adjacent to a wall hit.
function Effects.splash(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local signal = ctx.hitDetection.impactSignal :: BindableEvent?
    if signal == nil then
        warn(string.format(
            "[gameplay/effects] Splash needs ctx.hitDetection.impactSignal "
            .. "on %s",
            container:GetFullName()
        ))
        return
    end
    local capValue = cap.value :: number
    local radiusUnity = cap.radius_unity :: number
    local radiusStuds = radiusUnity * _STUDS_PER_METER
    signal.Event:Connect(function(_hitInstance: Instance, impactPos: Vector3)
        local value = _resolveValue(container, capValue, "damage")
        for _, player in ipairs(Players:GetPlayers()) do
            local char = player.Character
            local hrp = char and char:FindFirstChild("HumanoidRootPart")
            if hrp and hrp:IsA("BasePart") then
                if (hrp.Position - impactPos).Magnitude <= radiusStuds then
                    local charModel = hrp.Parent
                    if charModel and charModel:IsA("Model") then
                        _applyDamageToModel(charModel, value)
                    end
                end
            end
        end
        if container.Parent then container:Destroy() end
    end)
end

-- ``Effect.SpawnTemplate`` semantics:
--
--   On impact, clone ``ReplicatedStorage.Templates.<name>`` at the
--   impact CFrame. No-op when the template is absent (older outputs
--   without prefab packages). Clones are debris-cleaned after 2
--   seconds so the workspace doesn't accumulate.
--
-- Container destruction is delegated to whichever Effect runs LAST
-- in the tuple (validator-enforced ordering). For PlaneBullet the
-- ordering is SpawnTemplate → Splash → (no destroy here), so Splash's
-- container:Destroy handles it.
function Effects.spawnTemplate(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local signal = ctx.hitDetection.impactSignal :: BindableEvent?
    if signal == nil then
        warn(string.format(
            "[gameplay/effects] SpawnTemplate needs ctx.hitDetection.impactSignal "
            .. "on %s",
            container:GetFullName()
        ))
        return
    end
    local name = cap.name :: string
    signal.Event:Connect(function(_hitInstance: Instance, impactPos: Vector3)
        local templates = ReplicatedStorage:FindFirstChild("Templates")
        local template = templates and templates:FindFirstChild(name)
        if template == nil then
            return
        end
        local clone = template:Clone()
        local placeCFrame = CFrame.new(impactPos)
        if clone:IsA("Model") then
            clone:PivotTo(placeCFrame)
        elseif clone:IsA("BasePart") then
            clone.CFrame = placeCFrame
        end
        clone.Parent = workspace
        Debris:AddItem(clone, 2)
    end)
end

Composer.register("effect.damage", Effects.damage)
Composer.register("effect.splash", Effects.splash)
Composer.register("effect.spawn_template", Effects.spawnTemplate)

return Effects
