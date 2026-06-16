local STUDS_PER_METER = 3.571

local function randomInsideUnitSphere()
    -- rejection-sample a point inside the unit sphere (Unity Random.insideUnitSphere)
    while true do
        local v = Vector3.new(math.random() * 2 - 1, math.random() * 2 - 1, math.random() * 2 - 1)
        if v.Magnitude <= 1 then
            return v
        end
    end
end

local function randomOnUnitSphere()
    -- a uniformly random unit vector (Unity Random.onUnitSphere)
    local v
    repeat
        v = Vector3.new(math.random() * 2 - 1, math.random() * 2 - 1, math.random() * 2 - 1)
    until v.Magnitude > 0.0001
    return v.Unit
end

local function randomRotation()
    -- Unity Random.rotation → a CFrame with random orientation
    return CFrame.Angles(math.random() * math.pi * 2, math.random() * math.pi * 2, math.random() * math.pi * 2)
end

local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    -- Scalars only; debrisPrefabs/firePrefab are injected on self before Awake.
    self.numDebrisPieces = config.numDebrisPieces or 0
    self.numFires = config.numFires or 0
    return self
end

function Class:Start()
    -- Unity's `IEnumerator Start()` runs as a coroutine; lifecycle-scoped.
    self.host.startCoroutine(self, function()
        local multiplier = self:GetComponent("ParticleSystemMultiplier").multiplier

        for n = 0, self.numDebrisPieces * multiplier - 1 do
            local prefab = self.debrisPrefabs[math.random(1, #self.debrisPrefabs)]
            local pos = self.gameObject:GetPivot().Position
                + randomInsideUnitSphere() * (3 * multiplier * STUDS_PER_METER)
            local rot = randomRotation()
            self.host.instantiatePrefab(prefab, nil, CFrame.new(pos) * rot)
        end

        -- wait one frame so these new objects can be picked up in the overlapsphere function
        task.wait()

        local r = 10 * multiplier
        local origin = self.gameObject:GetPivot().Position
        local cols = workspace:GetPartBoundsInRadius(origin, r * STUDS_PER_METER)
        for _, col in cols do
            if self.numFires > 0 then
                -- col.Raycast(fireRay, out fireHit, r): cast from explosion toward this collider only
                local rp = RaycastParams.new()
                rp.FilterType = Enum.RaycastFilterType.Include
                rp.FilterDescendantsInstances = { col }
                local dir = col.Position - origin
                local fireHit = workspace:Raycast(origin, dir.Unit * (r * STUDS_PER_METER), rp)
                if fireHit then
                    self:AddFire(col, fireHit.Position, fireHit.Normal)
                    self.numFires = self.numFires - 1
                end
            end
        end

        local testR = 0
        while self.numFires > 0 and testR < r do
            local rayOrigin = self.gameObject:GetPivot().Position + Vector3.new(0, 1 * STUDS_PER_METER, 0)
            local dir = randomOnUnitSphere()
            local fireHit = workspace:Raycast(rayOrigin, dir * (testR * STUDS_PER_METER), RaycastParams.new())
            if fireHit then
                self:AddFire(nil, fireHit.Position, fireHit.Normal)
                self.numFires = self.numFires - 1
            end
            testR = testR + r * 0.1
        end
    end)
end

function Class:AddFire(t, pos, normal)
    pos = pos + normal * (0.5 * STUDS_PER_METER)
    -- Instantiate(firePrefab, pos, identity) then reparent to t → pass t as the parent.
    self.host.instantiatePrefab(self.firePrefab, t, CFrame.new(pos))
end

return Class