local Obstacle = require(game:GetService("ReplicatedStorage"):FindFirstChild("Obstacle", true) or game:GetService("ServerStorage"):FindFirstChild("Obstacle", true))
local TrackManager = require(game:GetService("ReplicatedStorage"):FindFirstChild("TrackManager", true) or game:GetService("ServerStorage"):FindFirstChild("TrackManager", true))

local STUDS_PER_METER = 3.571

-- static int s_DeathHash = Animator.StringToHash("Death") -> attribute name.
local s_DeathHash = "Death"
-- static int s_RunHash = Animator.StringToHash("Run") -> attribute name.
local s_RunHash = "Run"

local k_LeftMostLaneIndex = -1
local k_RightMostLaneIndex = 1
local k_Speed = 5.0

local Missile = {}
Missile.__index = Missile

function Missile.new(config)
	local self = setmetatable({}, Missile)
	-- animator (Animator) and movingSound (AudioClip[]) are reference/asset fields:
	-- injected on self before Awake. m_Audio is resolved in Awake via GetComponent.
	-- Constructor reads scalar config only and seeds internal state.
	self.m_OwnSegement = nil
	self.m_Ready = false
	self.m_IsMoving = false
	self.m_Audio = nil
	return self
end

function Missile:Awake()
	-- m_Audio = GetComponent<AudioSource>();
	self.m_Audio = self:GetComponent("AudioSource")
end

-- public override IEnumerator Spawn(TrackSegment segment, float t)
-- Launched via self.host.startCoroutine by TrackManager.SpawnFromAssetReference.
function Missile:Spawn(segment, t)
	-- Random.Range(int min, int max) is exclusive of max; k_RightMostLaneIndex + 1
	-- exclusive == k_RightMostLaneIndex inclusive for math.random.
	local lane = math.random(k_LeftMostLaneIndex, k_RightMostLaneIndex)

	-- segment.GetPointAt(t, out position, out rotation) -> (pos, rot CFrame).
	local position, rotation = segment:GetPointAt(t)

	-- Addressables.InstantiateAsync(gameObject.name, position, rotation) ->
	-- host.instantiatePrefab keyed by the addressable name (same precedent as
	-- TrackManager.Begin instantiating a character by name).
	local obj = self.host.instantiatePrefab(self.gameObject.Name, nil, rotation.Rotation + position)
	if obj == nil then
		warn(string.format("Unable to load obstacle %s.", self.gameObject.Name))
		return
	end

	-- obj.transform.SetParent(segment.objectRoot, true);
	obj.Parent = segment.objectRoot

	-- obj.transform.position += obj.transform.right * lane * laneOffset (metres -> studs).
	local _laneDelta = obj:GetPivot().RightVector * (lane * segment.manager.laneOffset * STUDS_PER_METER)
	obj:PivotTo(obj:GetPivot() + _laneDelta)

	-- obj.transform.forward = -obj.transform.forward (180 degree flip about up).
	obj:PivotTo(obj:GetPivot() * CFrame.Angles(0, math.rad(180), 0))

	-- Missile missile = obj.GetComponent<Missile>(); missile.m_OwnSegement = segment;
	local missile = self.host.getComponent(obj, "Missile")
	missile.m_OwnSegement = segment

	-- TODO hack (#issue7): nudge by Vector3.back then restore (net no-op).
	local oldPos = obj:GetPivot().Position
	obj:PivotTo(obj:GetPivot() + Vector3.new(0, 0, 1)) -- Vector3.back -> Roblox +Z
	obj:PivotTo(obj:GetPivot().Rotation + oldPos)

	missile:Setup()
end

-- public override void Setup()
function Missile:Setup()
	self.m_Ready = true
end

-- public override void Impacted()
function Missile:Impacted()
	-- base.Impacted();
	Obstacle.Impacted(self)

	if self.animator ~= nil then
		-- animator.SetTrigger(s_DeathHash): inert attribute record (no skeleton).
		self.gameObject:SetAttribute(s_DeathHash, true)
	end
end

function Missile:Update(dt)
	if self.m_Ready and self.m_OwnSegement.manager.isMoving then
		if self.m_IsMoving then
			-- transform.position += transform.forward * k_Speed * Time.deltaTime (m -> studs).
			local _delta = self.gameObject:GetPivot().LookVector * (k_Speed * STUDS_PER_METER * dt)
			self.gameObject:PivotTo(self.gameObject:GetPivot() + _delta)
		else
			-- C# segments[1] (0-indexed second segment) -> Luau segments[2].
			if TrackManager.instance.segments[2] == self.m_OwnSegement then
				if self.animator ~= nil then
					-- animator.SetTrigger(s_RunHash): inert attribute record.
					self.gameObject:SetAttribute(s_RunHash, true)
				end

				if self.m_Audio ~= nil and self.movingSound ~= nil and #self.movingSound > 0 then
					self.m_Audio.SoundId = self.movingSound[math.random(1, #self.movingSound)]
					self.m_Audio:Play()
					self.m_Audio.Looped = true
				end

				self.m_IsMoving = true
			end
		end
	end
end

return Missile