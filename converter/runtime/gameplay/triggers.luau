-- triggers.luau: Trigger-family capability handlers.
--
-- Owns the ``ctx.trigger`` namespace. Other families read but never
-- write to it.

local Composer = require(script.Parent:WaitForChild("Composer"))

local Triggers = {}

-- Normalize an attribute value to a strict bool via Lua truthiness.
-- Missing / nil → false. Any non-nil-non-false value → true. Matches
-- the design doc's normalization rules for ``Trigger.OnBoolAttribute``.
local function _normalize(value: any): boolean
    if value == nil or value == false then
        return false
    end
    return true
end

-- ``Trigger.OnBoolAttribute``: watch the named attribute on the
-- container and publish the normalized bool to ``ctx.trigger.value``
-- on bind and every change.
--
-- Idempotent: the composer's ``_GameplayBound`` marker prevents a
-- second binding on the same container. We DO NOT need to guard
-- here as well; the registry-level guard is sufficient.
function Triggers.onBoolAttribute(container: Instance, cap: {[string]: any}, ctx: {[string]: any}): ()
    local name = cap.name :: string
    -- ctx.trigger.value:   current normalized bool (read by movement,
    --                      effects, etc).
    -- ctx.trigger.changed: BindableEvent fired AFTER value is updated.
    --                      Downstream capabilities react to this rather
    --                      than re-subscribing to the underlying Roblox
    --                      attribute signal, so they don't need to know
    --                      what backs the trigger.
    local changed = Instance.new("BindableEvent")
    ctx.trigger.value = _normalize(container:GetAttribute(name))
    ctx.trigger.changed = changed
    container:GetAttributeChangedSignal(name):Connect(function()
        ctx.trigger.value = _normalize(container:GetAttribute(name))
        changed:Fire(ctx.trigger.value)
    end)
end

Composer.register("trigger.on_bool_attribute", Triggers.onBoolAttribute)

return Triggers
