local Class = {}
Class.__index = Class

local Player = require(game:GetService("ReplicatedStorage"):FindFirstChild("Player", true) or game:GetService("ServerStorage"):FindFirstChild("Player", true))

local STUDS_PER_METER = 3.571

function Class.new(config)
    local self = setmetatable({}, Class)
    config = config or {}
    -- Scalars only; never touch self.host / GetComponent here.
    -- [Range(3f, 10f)] public float shootTimer = 5f
    self.shootTimer = config.shootTimer or 5

    -- private Transform target
    self.target = nil
    return self
end

function Class:Start()
    -- target = Player.instance.transform
    self.target = Player.instance

    -- StartCoroutine(TimedShooting())
    self.host.startCoroutine(self, function()
        while self.target ~= nil do
            task.wait(self.shootTimer)

            -- Instantiate(ammoPrefab, origin.position, origin.rotation)
            local origin = self:_origin()
            if origin and self.ammoPrefab then
                self.host.instantiatePrefab(self.ammoPrefab, workspace, origin:GetPivot())
            end

            -- source.pitch = Random.Range(0.8f, 1.2f); source.PlayOneShot(shootSound); source.pitch = 1
            local pitch = math.random() * 0.4 + 0.8
            self:_playOneShot(self.shootSound, pitch)
        end
    end)
end

function Class:FixedUpdate(dt)
    if self.target ~= nil then
        -- origin.LookAt(target.position + Vector3.up * 2)
        local origin = self:_origin()
        if origin then
            local originPos = origin:GetPivot().Position
            local targetPos = self.target:GetPivot().Position + Vector3.new(0, 2 * STUDS_PER_METER, 0)
            origin:PivotTo(CFrame.lookAt(originPos, targetPos))
        end
    end
end

-- private Transform origin { get { return transform.Find("Origin"); } }
function Class:_origin()
    return self.gameObject:FindFirstChild("Origin")
end

-- private AudioSource source { get { return GetComponent<AudioSource>(); } }
-- PlayOneShot(clip) with a per-shot pitch, emulating AudioSource.pitch + PlayOneShot.
function Class:_playOneShot(soundId, pitch)
    if not soundId then return end
    local s = Instance.new("Sound")
    s.SoundId = soundId
    s.PlaybackSpeed = pitch or 1
    local part = self.gameObject
    if part and part:IsA("BasePart") then
        s.Parent = part
    else
        s.Parent = game:GetService("SoundService")
    end
    s:Play()
    s.Ended:Connect(function() s:Destroy() end)
    task.delay(5, function() if s and s.Parent then s:Destroy() end end)
end

return Class