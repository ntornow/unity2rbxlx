-- UnityBridge/Time
-- Maps Unity's Time class to Roblox equivalents.
-- Usage:
--   local Time = require(ReplicatedStorage.UnityBridge.Time)
--   local speed = distance / Time.deltaTime

local RunService = game:GetService("RunService")

local Time = {}

Time.deltaTime = 0
Time.time = 0
Time.fixedDeltaTime = 1/60
Time.timeScale = 1
Time._startTime = os.clock()

RunService.Heartbeat:Connect(function(dt)
	Time.deltaTime = dt * Time.timeScale
	Time.time = os.clock() - Time._startTime
end)

return Time
