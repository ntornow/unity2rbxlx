-- lifetime.luau: Lifetime-family capability handlers.
--
-- Owns the ``ctx.lifetime`` namespace.
--
-- PR #73a ships ``Lifetime.Persistent`` only. ``Despawn`` and
-- ``OnFirstHit`` arrive in PR #73b.

local Composer = require(script.Parent:WaitForChild("Composer"))

local Lifetime = {}

-- ``Lifetime.Persistent`` is a no-op handler. It exists so the
-- Behavior IR records the lifetime decision explicitly rather than
-- leaving it implicit. The composer dispatch still runs through this
-- handler so an unknown-kind warning doesn't fire.
function Lifetime.persistent(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    -- intentionally empty
end

Composer.register("lifetime.persistent", Lifetime.persistent)

return Lifetime
