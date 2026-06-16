local Obstacle = {}
Obstacle.__index = Obstacle

function Obstacle.new(config)
	local self = setmetatable({}, Obstacle)
	-- impactedSound (AudioClip) is an asset field: injected on self as a
	-- rbxassetid string before Awake. Constructor reads scalar config only.
	return self
end

-- public virtual void Setup() {} — no-op in the base; derived obstacles override.
function Obstacle:Setup()
end

-- public abstract IEnumerator Spawn(TrackSegment segment, float t);
-- Abstract: the base provides no body. Derived obstacles (AllLaneObstacle,
-- Missile, SimpleBarricade, PatrollingObstacle) implement Spawn as a coroutine
-- driven via self.host.startCoroutine.
function Obstacle:Spawn(segment, t)
end

function Obstacle:Impacted()
	-- Animation anim = GetComponentInChildren<Animation>();
	-- Legacy Animation is the closest Roblox analog of AnimationController.
	local anim = self.gameObject:FindFirstChildWhichIsA("AnimationController", true)

	-- AudioSource audioSource = GetComponent<AudioSource>();
	local audioSource = self:GetComponent("AudioSource")

	if anim ~= nil then
		-- anim.Play(): legacy skeletal/clip animation has no Roblox runtime
		-- equivalent; record the intent inertly instead of driving a skeleton.
		self.gameObject:SetAttribute("ImpactAnimationPlayed", true)
	end

	if audioSource ~= nil and self.impactedSound ~= nil then
		audioSource:Stop()
		audioSource.Looped = false
		audioSource.SoundId = self.impactedSound
		audioSource:Play()
	end
end

return Obstacle