local STUDS_PER_METER = 3.571

local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    self.explosionForce = config.explosionForce or 4
    return self
end

function Class:Start()
    -- Unity's Start() returns IEnumerator (a coroutine); preserve the one-frame wait.
    self.host.startCoroutine(self, function()
        -- wait one frame because some explosions instantiate debris which should then
        -- be pushed by physics force
        task.wait()  -- yield return null

        local multiplier = self:GetComponent("ParticleSystemMultiplier").multiplier

        local r = 10 * multiplier  -- Unity metres
        local rStuds = r * STUDS_PER_METER
        local origin = self.gameObject:GetPivot().Position

        -- Physics.OverlapSphere(transform.position, r)
        local cols = workspace:GetPartBoundsInRadius(origin, rStuds)

        -- Dedup by physics body: Roblox parts in one assembly share AssemblyRootPart;
        -- an anchored body has no dynamics (analogous to a null attachedRigidbody).
        local rigidbodies = {}
        for _, col in cols do
            local rb = col.AssemblyRootPart
            if rb ~= nil and not rb.Anchored and not table.find(rigidbodies, rb) then
                table.insert(rigidbodies, rb)
            end
        end

        local upwardsModifier = 1 * multiplier  -- Unity metres
        for _, rb in rigidbodies do
            -- rb.AddExplosionForce(explosionForce*multiplier, transform.position, r, 1*multiplier, ForceMode.Impulse)
            -- Unity shifts the explosion origin down by upwardsModifier so bodies are pushed upward,
            -- and the impulse falls off linearly to zero at the radius edge.
            local explosionPos = origin - Vector3.new(0, upwardsModifier * STUDS_PER_METER, 0)
            local dir = rb.Position - explosionPos
            local dist = dir.Magnitude
            if dist > 0 then
                local falloff = 1 - (dist / rStuds)
                if falloff < 0 then falloff = 0 end
                local impulse = dir.Unit * (self.explosionForce * multiplier * falloff)
                rb:ApplyImpulse(impulse)
            end
        end
    end)
end

return Class