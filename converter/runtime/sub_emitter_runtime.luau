-- SubEmitterRuntime: Emulates Unity particle SubEmitters in Roblox.
--
-- Scans workspace for ParticleEmitters with _HasSubEmitters attribute
-- and sets up burst/death/birth sub-emission behavior.
--
-- Trigger types:
--   Birth: spawns sub-particles when parent emitter emits (rate-based burst)
--   Death: spawns sub-particles at end of parent particle lifetime
--   Collision: spawns sub-particles when parent particles hit surfaces
--
-- Since Roblox ParticleEmitter doesn't have per-particle events,
-- we approximate:
--   Birth → immediate burst on Enable
--   Death → delayed burst after Lifetime
--   Collision → not implemented (no per-particle collision in Roblox)

local CollectionService = game:GetService("CollectionService")

local function setupSubEmitters()
	for _, part in workspace:GetDescendants() do
		if not part:IsA("BasePart") then continue end

		for _, emitter in part:GetChildren() do
			if not emitter:IsA("ParticleEmitter") then continue end
			if not emitter:GetAttribute("_HasSubEmitters") then continue end

			local triggers = emitter:GetAttribute("UnitySubEmitterTriggers") or ""
			local subCount = emitter:GetAttribute("UnitySubEmitterCount") or 0

			if subCount <= 0 then continue end

			-- Create a sub-emitter (burst effect) as a child of the same part
			local subEmitter = Instance.new("ParticleEmitter")
			subEmitter.Name = emitter.Name .. "_Sub"
			subEmitter.Rate = 0 -- Only emit via :Emit()
			subEmitter.Lifetime = NumberRange.new(0.2, 0.5)
			subEmitter.Speed = NumberRange.new(2, 8)
			subEmitter.SpreadAngle = Vector2.new(180, 180) -- Burst in all directions
			subEmitter.Size = NumberSequence.new({
				NumberSequenceKeypoint.new(0, emitter.Size.Keypoints[1].Value * 0.5),
				NumberSequenceKeypoint.new(1, 0),
			})
			subEmitter.Transparency = NumberSequence.new({
				NumberSequenceKeypoint.new(0, 0),
				NumberSequenceKeypoint.new(1, 1),
			})
			subEmitter.Color = emitter.Color -- Inherit parent color
			subEmitter.Parent = part

			for trigger in string.split(triggers, ",") do
				trigger = string.match(trigger, "^%s*(.-)%s*$") -- trim

				if trigger == "Birth" then
					-- Burst when parent emitter is enabled
					emitter:GetPropertyChangedSignal("Enabled"):Connect(function()
						if emitter.Enabled then
							subEmitter:Emit(math.max(1, subCount * 3))
						end
					end)
					-- Initial burst
					if emitter.Enabled then
						task.defer(function()
							subEmitter:Emit(math.max(1, subCount * 3))
						end)
					end

				elseif trigger == "Death" then
					-- Approximate: burst after parent particle lifetime elapses
					local lifetime = emitter.Lifetime.Max
					if emitter.Enabled and lifetime > 0 then
						task.spawn(function()
							while emitter.Enabled do
								task.wait(lifetime)
								if emitter.Enabled then
									subEmitter:Emit(math.max(1, subCount * 5))
								end
							end
						end)
					end

				elseif trigger == "Collision" then
					-- Can't detect per-particle collision in Roblox
					-- Approximate: periodic bursts while emitter is active
					task.spawn(function()
						while emitter.Enabled do
							task.wait(0.5)
							if emitter.Enabled and emitter.Rate > 0 then
								subEmitter:Emit(math.max(1, subCount))
							end
						end
					end)
				end
			end
		end
	end
end

-- Wait for workspace to load, then set up
task.defer(function()
	task.wait(2)
	setupSubEmitters()
end)
