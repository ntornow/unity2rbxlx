local Obstacle = require(game:GetService("ReplicatedStorage"):FindFirstChild("Obstacle", true) or game:GetService("ServerStorage"):FindFirstChild("Obstacle", true))

-- protected const float k_LaneOffsetToFullWidth = 2f;
local k_LaneOffsetToFullWidth = 2

-- Mathf.PingPong(t, length): triangle wave bouncing between 0 and length.
local function pingPong(t, length)
	local span = length * 2
	local repeated = t - math.floor(t / span) * span
	return length - math.abs(repeated - length)
end

local PatrollingObstacle = {}
PatrollingObstacle.__index = PatrollingObstacle

function PatrollingObstacle.new(config)
	local self = setmetatable({}, PatrollingObstacle)
	-- Scalar serialized fields. patrollingSound (AudioClip[]) and impactedSound
	-- (inherited) are asset fields injected on self before Awake; animator is a
	-- component reference resolved in Awake. Constructor reads scalar config only.
	self.minTime = config.minTime or 2
	self.maxTime = config.maxTime or 5

	self.m_Segement = nil
	self.m_OriginalPosition = Vector3.new(0, 0, 0)
	self.m_MaxSpeed = 0
	self.m_CurrentPos = 0
	self.m_Audio = nil
	self.m_isMoving = false
	self.animator = nil
	return self
end

function PatrollingObstacle:Awake()
	-- public Animator animator ("Leave empty if no animation"): resolve the peer
	-- Animator component if one exists; nil leaves the animation paths inert.
	self.animator = self:GetComponent("Animator")
end

-- transform.localPosition read: position relative to the parent's pivot.
function PatrollingObstacle:_getLocalPosition()
	local parentCF = self.gameObject.Parent:GetPivot()
	return parentCF:PointToObjectSpace(self.gameObject:GetPivot().Position)
end

-- transform.localPosition write: keep current rotation, place at the parent-local point.
function PatrollingObstacle:_setLocalPosition(v)
	local parentCF = self.gameObject.Parent:GetPivot()
	local worldPos = parentCF:PointToWorldSpace(v)
	self.gameObject:PivotTo(CFrame.new(worldPos) * self.gameObject:GetPivot().Rotation)
end

function PatrollingObstacle:Spawn(segment, t)
	-- segment.GetPointAt(t, out position, out rotation): GetPointAt returns (pos, rot).
	local position, rotation = segment:GetPointAt(t)

	-- Addressables.InstantiateAsync(gameObject.name, position, rotation): instantiate a
	-- fresh copy of this obstacle by its addressable name. No serialized prefab_id is
	-- injected for an addressables-by-name spawn, so resolve the template instance from
	-- ReplicatedStorage.Templates (the converter's prefab-template store).
	local templatesFolder = game:GetService("ReplicatedStorage"):FindFirstChild("Templates")
	local template = templatesFolder and templatesFolder:FindFirstChild(self.gameObject.Name)

	-- yield return op (await the async instantiate); a Clone resolves synchronously.
	if template == nil then
		-- op.Result == null || !(op.Result is GameObject)
		warn(string.format("Unable to load obstacle %s.", self.gameObject.Name))
		return
	end

	local obj = template:Clone()
	obj:PivotTo(CFrame.new(position) * rotation.Rotation)

	-- obj.transform.SetParent(segment.objectRoot, true): keep world transform.
	obj.Parent = segment.objectRoot

	-- PatrollingObstacle po = obj.GetComponent<PatrollingObstacle>(): attach and run the
	-- behaviour on the freshly spawned object; the host returns the live component.
	local po = self.host.addComponent(obj, "PatrollingObstacle", {
		minTime = self.minTime,
		maxTime = self.maxTime,
	})
	po.m_Segement = segment

	-- TODO : remove that hack related to #issue7 — nudge then restore the position.
	local pivot = obj:GetPivot()
	local oldPos = pivot.Position
	obj:PivotTo(pivot + Vector3.new(0, 0, 1)) -- += Vector3.back (Unity (0,0,-1) → Roblox +Z)
	obj:PivotTo(CFrame.new(oldPos) * pivot.Rotation)

	po:Setup()
end

function PatrollingObstacle:Setup()
	-- m_Audio = GetComponent<AudioSource>();
	self.m_Audio = self:GetComponent("AudioSource")
	if self.m_Audio ~= nil and self.patrollingSound ~= nil and #self.patrollingSound > 0 then
		self.m_Audio.Looped = true
		-- m_Audio.clip = patrollingSound[Random.Range(0, patrollingSound.Length)];
		self.m_Audio.SoundId = self.patrollingSound[math.random(1, #self.patrollingSound)]
		self.m_Audio:Play()
	end

	-- m_OriginalPosition = transform.localPosition + transform.right * m_Segement.manager.laneOffset;
	local laneOffset = self.m_Segement.manager.laneOffset
	self.m_OriginalPosition = self:_getLocalPosition() + self.gameObject:GetPivot().RightVector * laneOffset
	self:_setLocalPosition(self.m_OriginalPosition)

	-- float actualTime = Random.Range(minTime, maxTime); (float overload)
	local actualTime = math.random() * (self.maxTime - self.minTime) + self.minTime

	-- time 2, because the animation is a back & forth, so we need the speed needed to do
	-- 4 lanes offset in the given time.
	self.m_MaxSpeed = (laneOffset * k_LaneOffsetToFullWidth * 2) / actualTime

	if self.animator ~= nil then
		-- AnimationClip clip = animator.GetCurrentAnimatorClipInfo(0)[0].clip;
		-- animator.SetFloat(s_SpeedRatioHash, clip.length / actualTime);
		-- Skeletal/clip animation has no Roblox runtime; clip.length is unavailable, so
		-- record the SpeedRatio parameter inertly keyed on the available timing.
		self.gameObject:SetAttribute("SpeedRatio", 1 / actualTime)
	end

	self.m_isMoving = true
end

function PatrollingObstacle:Impacted()
	self.m_isMoving = false
	-- base.Impacted();
	Obstacle.Impacted(self)

	if self.animator ~= nil then
		-- animator.SetTrigger(s_DeadHash): record the Dead trigger inertly.
		self.gameObject:SetAttribute("Dead", true)
	end
end

function PatrollingObstacle:Update(dt)
	if not self.m_isMoving then
		return
	end

	-- m_CurrentPos += Time.deltaTime * m_MaxSpeed;
	self.m_CurrentPos = self.m_CurrentPos + dt * self.m_MaxSpeed

	local laneOffset = self.m_Segement.manager.laneOffset
	local offset = pingPong(self.m_CurrentPos, laneOffset * k_LaneOffsetToFullWidth)
	-- transform.localPosition = m_OriginalPosition - transform.right * Mathf.PingPong(...);
	self:_setLocalPosition(self.m_OriginalPosition - self.gameObject:GetPivot().RightVector * offset)
end

return PatrollingObstacle