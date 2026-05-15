local multiplier = 1

local container = script.Parent

local function scaleNumberSequence(seq, factor)
	local keypoints = {}
	for _, kp in ipairs(seq.Keypoints) do
		table.insert(keypoints, NumberSequenceKeypoint.new(kp.Time, kp.Value * factor, kp.Envelope * factor))
	end
	return NumberSequence.new(keypoints)
end

local function scaleNumberRange(range, factor)
	return NumberRange.new(range.Min * factor, range.Max * factor)
end

local lifetimeFactor = multiplier + (1 - multiplier) * 0.5

for _, emitter in ipairs(container:GetDescendants()) do
	if emitter:IsA("ParticleEmitter") then
		emitter.Size = scaleNumberSequence(emitter.Size, multiplier)
		emitter.Speed = scaleNumberRange(emitter.Speed, multiplier)
		emitter.Lifetime = scaleNumberRange(emitter.Lifetime, lifetimeFactor)
		emitter:Clear()
		emitter.Enabled = true
	end
end