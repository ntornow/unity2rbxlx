local Class = {}
Class.__index = Class

local STUDS_PER_METER = 3.571

function Class.new(config)
    local self = setmetatable({}, Class)
    self.explodeTime = config.explodeTime or 1
    self.damage = config.damage or 10
    return self
end

function Class:Awake()
    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        local plr = self.host.playerFromTouch(other)
        if not plr then return end
        local sound = self:GetComponent("AudioSource")
        if sound then sound:Play() end
        self.host.invoke(self, "Explode", self.explodeTime)
    end)
end

function Class:Explode()
    local pos = self.gameObject:GetPivot().Position
    self.host.instantiatePrefab(self.explosion, workspace, CFrame.new(pos))

    local cols = workspace:GetPartBoundsInRadius(pos, 2 * STUDS_PER_METER)
    for _, col in cols do
        local plr = self.host.playerFromTouch(col)
        if plr then
            local char = plr.Character
            local humanoid = char and char:FindFirstChildOfClass("Humanoid")
            if humanoid then
                humanoid:TakeDamage(self.damage)
            end
        end
    end

    self.host.destroy(self.gameObject)
end

return Class