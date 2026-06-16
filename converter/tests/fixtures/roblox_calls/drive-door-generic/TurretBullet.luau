local TurretBullet = {}
TurretBullet.__index = TurretBullet

function TurretBullet.new(config)
    local self = setmetatable({}, TurretBullet)
    self.fadeTime = config.fadeTime or 3
    self.force = config.force or 60
    self.damage = config.damage or 10
    return self
end

function TurretBullet:Start()
    local rb = self:GetComponent("Rigidbody")
    if rb then
        -- AddRelativeForce(Vector3.forward * force, ForceMode.Impulse): local forward impulse
        rb:ApplyImpulse(self.gameObject:GetPivot().LookVector * self.force)
    end

    -- Destroy(gameObject, fadeTime): bound to target lifetime, raw task.delay is intentional
    task.delay(self.fadeTime, function()
        self.host.destroy(self.gameObject)
    end)
end

function TurretBullet:Awake()
    -- OnCollisionEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        local plr = self.host.playerFromTouch(other)
        if not plr then return end  -- other.collider.tag == "Player"

        -- other.collider.SendMessage("TakeDamage", damage)
        local char = plr.Character
        local humanoid = char and char:FindFirstChildOfType("Humanoid")
        if humanoid then
            humanoid:TakeDamage(self.damage)
        end

        self.host.destroy(self.gameObject)
    end)
end

return TurretBullet