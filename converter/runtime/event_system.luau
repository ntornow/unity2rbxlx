-- EventSystem: Unity EventSystem equivalent for Roblox UI.
--
-- Provides pointer enter/exit/click events and selection management,
-- mirroring Unity's EventSystem + GraphicRaycaster behaviour.
--
-- Usage:
--   local EventSystem = require(path.to.EventSystem)
--   local es = EventSystem.new()
--   es:On("PointerClick", someGuiButton, function(target)
--       print("Clicked:", target.Name)
--   end)
--   -- Call es:Destroy() when done to disconnect all listeners.

local UserInputService = game:GetService("UserInputService")
local Players = game:GetService("Players")

local EventSystem = {}
EventSystem.__index = EventSystem

--- Create a new EventSystem and begin listening for input.
--- @return EventSystem
function EventSystem.new()
	local self = setmetatable({}, EventSystem)
	self.selectedObject = nil
	self.hovered = nil
	self.callbacks = {}
	self._connections = {}
	self:_setup()
	return self
end

--- Wire up mouse and keyboard listeners.
function EventSystem:_setup()
	local player = Players.LocalPlayer
	if not player then
		return
	end

	local mouse = player:GetMouse()

	-- Pointer move / hover tracking
	table.insert(self._connections, mouse.Move:Connect(function()
		local target = mouse.Target
		if target ~= self.hovered then
			if self.hovered then
				self:_fire("PointerExit", self.hovered)
			end
			self.hovered = target
			if target then
				self:_fire("PointerEnter", target)
			end
		end
	end))

	-- Pointer click
	table.insert(self._connections, mouse.Button1Down:Connect(function()
		if self.hovered then
			self:_fire("PointerClick", self.hovered)
			self:Select(self.hovered)
		end
	end))

	-- Keyboard navigation (Tab to cycle selection)
	table.insert(self._connections, UserInputService.InputBegan:Connect(function(input, processed)
		if processed then
			return
		end
		if input.KeyCode == Enum.KeyCode.Tab then
			self:_navigateNext()
		end
	end))
end

--- Set the currently selected GUI object.
--- Fires Deselect on the previous selection and Select on the new one.
--- @param guiObject Instance? -- The object to select, or nil to deselect.
function EventSystem:Select(guiObject)
	if self.selectedObject then
		self:_fire("Deselect", self.selectedObject)
	end
	self.selectedObject = guiObject
	if guiObject then
		self:_fire("Select", guiObject)
	end
end

--- Register a callback for an event on a specific target.
--- @param eventName string -- e.g. "PointerClick", "PointerEnter", "Select".
--- @param target Instance -- The GUI object or BasePart to listen on.
--- @param callback function -- Called with (target) when the event fires.
function EventSystem:On(eventName, target, callback)
	local key = eventName .. "_" .. tostring(target)
	self.callbacks[key] = callback
end

--- Fire a named event on a target, invoking its registered callback.
--- @param eventName string
--- @param target Instance
function EventSystem:_fire(eventName, target)
	local key = eventName .. "_" .. tostring(target)
	local cb = self.callbacks[key]
	if cb then
		cb(target)
	end
end

--- Cycle selection to the next GUI element (stub for extensibility).
function EventSystem:_navigateNext()
	-- Simple tab navigation through GUI elements.
	-- Override or extend this for project-specific navigation order.
end

--- Disconnect all event listeners and clear state.
function EventSystem:Destroy()
	for _, conn in self._connections do
		conn:Disconnect()
	end
	table.clear(self._connections)
	table.clear(self.callbacks)
	self.selectedObject = nil
	self.hovered = nil
end

return EventSystem
