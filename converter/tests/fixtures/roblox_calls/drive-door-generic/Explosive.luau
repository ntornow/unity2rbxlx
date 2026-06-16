local Class = {}
Class.__index = Class

local STUDS_PER_METER = 3.571

function Class.new(config)
    local self = setmetatable({}, Class)
    -- Scalars only; prefab field (explosionPrefab) is injected on self before Awake.
    self.detonationImpactVelocity = config.detonationImpactVelocity or 10
    self.sizeMultiplier = config.sizeMultiplier or 1
    self.reset = config.reset
    if self.reset == nil then self.reset = true end
    self.resetTimeDelay = config.resetTimeDelay or 10
    self.m_Exploded = false
    self.m_ObjectResetter = nil
    return self
end

function Class:Awake()
    -- OnCollisionEnter(col)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        if not self.enabled then return end
        if typeof(other) ~= "Instance" or not other:IsA("BasePart") then return end

        -- Roblox .Touched gives no contact manifold, so approximate Unity's
        -- col.contacts[0] / col.relativeVelocity from the two bodies' positions
        -- and assembly velocities.
        local selfPart = self.gameObject:IsA("BasePart") and self.gameObject
            or self.gameObject:FindFirstChildWhichIsA("BasePart")
        local selfPos = self.gameObject:GetPivot().Position
        local otherPos = other.Position

        local selfVel = (selfPart and selfPart.AssemblyLinearVelocity) or Vector3.zero
        local otherVel = other.AssemblyLinearVelocity
        local relativeVelocity = selfVel - otherVel

        -- contact normal approximated as the direction between the two bodies
        local normal = selfPos - otherPos
        if normal.Magnitude > 0 then
            normal = normal.Unit
        else
            normal = Vector3.yAxis
        end
        local contactPoint = (selfPos + otherPos) / 2

        -- compare relative velocity to collision normal - so we don't explode
        -- from a fast but gentle glancing collision. Vector3.Project(v, n).magnitude.
        local velocityAlongCollisionNormal = math.abs(relativeVelocity:Dot(normal))

        -- detonationImpactVelocity is a Unity m/s threshold; Roblox velocities are studs/s.
        if velocityAlongCollisionNormal > self.detonationImpactVelocity * STUDS_PER_METER or self.m_Exploded then
            if not self.m_Exploded then
                if self.explosionPrefab then
                    -- Instantiate(explosionPrefab, contacts[0].point, LookRotation(normal))
                    local cframe = CFrame.lookAt(contactPoint, contactPoint + normal)
                    self.host.instantiatePrefab(self.explosionPrefab, workspace, cframe)
                end
                self.m_Exploded = true

                -- SendMessage("Immobilize"): Unity broadcasts to every component on
                -- this GameObject. Roblox has no name-dispatch; best-effort invoke
                -- "Immobilize" on this instance.
                if type(self.Immobilize) == "function" then
                    self:Immobilize()
                end

                if self.reset then
                    if self.m_ObjectResetter then
                        self.m_ObjectResetter:DelayedReset(self.resetTimeDelay)
                    end
                end
            end
        end
    end)
end

function Class:Start()
    self.m_ObjectResetter = self:GetComponent("ObjectResetter")
end

function Class:Reset()
    self.m_Exploded = false
end

return Class