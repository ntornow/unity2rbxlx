-- EventDispatch: cross-class connect helper for client event listeners.
--
-- Connect a handler to either a server-fired ``RemoteEvent`` or a
-- client-side ``BindableEvent`` without the caller having to know
-- which produced it. The two classes expose different event members
-- (``RemoteEvent.OnClientEvent`` vs ``BindableEvent.Event``), so a
-- caller that hard-codes one crashes the moment the producer side
-- swaps implementations.
--
-- Parented at ``ReplicatedStorage.AutoFpsEventDispatch`` by FPS
-- scaffolding. The auto-generated HUDController requires it by this
-- exact name; ``AUTO_FPS_EVENT_DISPATCH_NAME`` in
-- ``converter/scaffolding/fps.py`` is the authoritative constant.
--
-- Usage:
--   local EventDispatch = require(
--       ReplicatedStorage:WaitForChild("AutoFpsEventDispatch")
--   )
--   EventDispatch.connectClient(remoteOrBindable, function(payload)
--       -- handler runs whether the producer is a RemoteEvent or BindableEvent
--   end)

local EventDispatch = {}

--- Connect *handler* to whichever event-member ``evt`` exposes.
--- Silently no-ops on ``nil`` so callers can pass the result of
--- ``ReplicatedStorage:WaitForChild(name, timeout)`` directly without
--- guarding for the timeout case.
function EventDispatch.connectClient(evt, handler)
	if not evt then
		return
	end
	if evt:IsA("BindableEvent") then
		evt.Event:Connect(handler)
	elseif evt:IsA("RemoteEvent") then
		evt.OnClientEvent:Connect(handler)
	end
end

return EventDispatch
