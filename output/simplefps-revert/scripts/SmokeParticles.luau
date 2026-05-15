-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local extinguishSounds = {}

local audioSource = script.Parent:FindFirstChildWhichIsA("Sound")
if audioSource and #extinguishSounds > 0 then
    audioSource.SoundId = extinguishSounds[math.random(1, #extinguishSounds)]
    audioSource:Play()
end