-- @@GAMEPLAY_RUNTIME_MODULE@@ converter-owned (EventDispatch)
-- EventDispatch: cross-class connect helper for client event listeners.
--
-- Connect a handler to either a server-fired ``RemoteEvent`` or a
-- client-side ``BindableEvent`` without the caller having to know
-- which produced it. The two classes expose different event members
-- (``RemoteEvent.OnClientEvent`` vs ``BindableEvent.Event``), so a
-- caller that hard-codes one crashes the moment the producer side
-- swaps implementations.
--
-- Parented at ``ReplicatedStorage.AutoGen.EventDispatch`` by the
-- pipeline (PR #75). A compat alias at
-- ``ReplicatedStorage.AutoFpsEventDispatch`` proxies to this module
-- via a ``WaitForChild`` chain so already-converted outputs that pin
-- the historic name keep resolving until PR #78 retires the alias.
--
-- Usage:
--   local EventDispatch = require(
--       ReplicatedStorage:WaitForChild("AutoGen"):WaitForChild("EventDispatch")
--   )
--   EventDispatch.connectClient(remoteOrBindable, function(payload)
--       -- handler runs whether the producer is a RemoteEvent or BindableEvent
--   end)

local EventDispatch = {}

--- Connect *handler* to whichever event-member ``evt`` exposes.
--- Silently no-ops on ``nil`` so callers can pass the result of
--- ``ReplicatedStorage:WaitForChild(name, timeout)`` directly without
--- guarding for the timeout case.
--- @param evt Instance | nil  RemoteEvent or BindableEvent (or nil)
--- @param handler (...any) -> ()  callback receiving the event payload
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
