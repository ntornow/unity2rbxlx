-- lifetime.luau: Lifetime-family capability handlers.
--
-- Owns the ``ctx.lifetime`` namespace.
--
-- Ships ``Lifetime.Persistent`` (PR #73a) and ``Lifetime.Despawn``
-- (PR #73b, projectile slice).

local Debris = game:GetService("Debris")
local Composer = require(script.Parent:WaitForChild("Composer"))

local Lifetime = {}

-- ``Lifetime.Persistent`` is a no-op handler. It exists so the
-- Behavior IR records the lifetime decision explicitly rather than
-- leaving it implicit. The composer dispatch still runs through this
-- handler so an unknown-kind warning doesn't fire.
function Lifetime.persistent(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    -- intentionally empty
end

-- ``Lifetime.Despawn`` schedules the container for destruction after
-- ``cap.seconds`` Unity seconds (1:1 with Roblox seconds — only
-- distances need stud-conversion). Maps to Unity ``Destroy(gameObject,
-- t)``. Mirrors the legacy bullet pack's
-- ``Debris:AddItem(container, fadeTime)``.
--
-- Per-instance override: ``fadeTime`` attribute on the container (or
-- its primary part for Model containers) takes precedence so Unity
-- inspector tuning survives — same pattern as Movement.Impulse and
-- Effect.Damage.
function Lifetime.despawn(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local attrHost: Instance = container
    if container:IsA("Model") then
        attrHost = container.PrimaryPart or container
    end
    local overrideFade = attrHost:GetAttribute("fadeTime")
    local seconds: number = (typeof(overrideFade) == "number" and overrideFade)
        or (cap.seconds :: number)
    Debris:AddItem(container, seconds)
end

Composer.register("lifetime.persistent", Lifetime.persistent)
Composer.register("lifetime.despawn", Lifetime.despawn)

return Lifetime
