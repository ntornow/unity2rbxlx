-- UnityBridge/Coroutine
-- Maps Unity's coroutine system to Roblox task library.
-- Usage:
--   local Coroutine = require(ReplicatedStorage.UnityBridge.Coroutine)
--   Coroutine.Start(function()
--       Coroutine.WaitForSeconds(2)
--       print("2 seconds later")
--   end)

local Coroutine = {}

function Coroutine.Start(fn, ...)
	return task.spawn(fn, ...)
end

function Coroutine.WaitForSeconds(seconds)
	task.wait(seconds)
end

function Coroutine.WaitForEndOfFrame()
	task.wait()
end

function Coroutine.Yield()
	task.wait()
end

return Coroutine
