local PlaneBullet = {}
PlaneBullet.__index = PlaneBullet

function PlaneBullet.new(config)
    local self = setmetatable({}, PlaneBullet)
    self.fadeTime = config.fadeTime or 6
    self.force = config.force or 200
    self.damage = config.damage or 10
    return self
end

function PlaneBullet:Awake()
    -- OnCollisionEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        local pos = self.gameObject:GetPivot().Position

        -- Instantiate(explosion, transform.position, Quaternion.identity)
        if self.explosion then
            self.host.instantiatePrefab(self.explosion, nil, CFrame.new(pos))
        end

        -- Physics.OverlapSphere(transform.position, 2) — Unity metres -> studs
        local cols = workspace:GetPartBoundsInRadius(pos, 2 * 3.571)
        local seen = {}
        for _, col in cols do
            local plr = self.host.playerFromTouch(col)
            if plr and plr.Character and not seen[plr] then
                seen[plr] = true
                -- col.SendMessage("TakeDamage", damage) on the Player
                local hum = plr.Character:FindFirstChildOfType("Humanoid")
                if hum then
                    hum:TakeDamage(self.damage)
                end
            end
        end

        -- Destroy(gameObject)
        self.host.destroy(self.gameObject)
    end)
end

function PlaneBullet:Start()
    -- rb.AddRelativeForce(Vector3.forward * force, ForceMode.Impulse)
    local rb = self:GetComponent("Rigidbody")
    if rb then
        rb:ApplyImpulse(self.gameObject:GetPivot().LookVector * self.force)
    end

    -- Destroy(gameObject, fadeTime): bound to TARGET lifetime, raw task.delay is intentional
    task.delay(self.fadeTime, function()
        self.host.destroy(self.gameObject)
    end)
end

return PlaneBullet