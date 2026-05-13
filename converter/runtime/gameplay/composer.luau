-- @@GAMEPLAY_RUNTIME_MODULE@@ converter-owned (Composer)
-- Composer: dispatch a Behavior's capability tuple to per-family
-- runtime modules. Wires the per-family ``ctx`` table through each
-- capability in tuple order. The capability dataflow contract is
-- enforced at emit time by ``converter.gameplay.composer`` on the
-- Python side; this module trusts the input.
--
-- Re-bind marker: the first call sets ``container:SetAttribute(
-- "_GameplayBound", true)`` so subsequent calls on the same container
-- are no-ops. One marker for the whole composer call — individual
-- capability modules don't each register their own.
--
-- Family registry: each family runtime registers a ``run(container,
-- cap, ctx)`` handler under its capability ``kind`` string. The
-- registry is built at module-init time; new families register by
-- calling ``Composer.register`` from their own module body.

local Composer = {}

local _registry: {[string]: (Instance, {[string]: any}, {[string]: any}) -> ()} = {}

-- Family registry entry point. ``kind`` is the capability's dotted
-- name (e.g. ``"trigger.on_bool_attribute"``); ``handler`` takes
-- (container, cap, ctx) and is responsible only for its own
-- ctx.<family> slice.
function Composer.register(kind: string, handler: (Instance, {[string]: any}, {[string]: any}) -> ()): ()
    _registry[kind] = handler
end

-- Bind every capability in ``capabilities`` to ``container``, in tuple
-- order. No-op if the container has already been bound (the
-- ``_GameplayBound`` marker is set on the container itself).
function Composer.run(container: Instance, capabilities: {{[string]: any}}): ()
    if container:GetAttribute("_GameplayBound") then
        return
    end
    local ctx = {
        movement = {},
        lifetime = {},
        hitDetection = {},
        effect = {},
        trigger = {},
    }
    for _, cap in ipairs(capabilities) do
        local kind = cap.kind
        local handler = _registry[kind]
        if handler == nil then
            warn(string.format(
                "[gameplay/composer] no handler registered for kind %q on %s",
                tostring(kind), container:GetFullName()
            ))
        else
            handler(container, cap, ctx)
        end
    end
    container:SetAttribute("_GameplayBound", true)
end

return Composer
