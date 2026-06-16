local STUDS_PER_METER = 3.571

local Pickup = {}
Pickup.__index = Pickup

function Pickup.new(config)
    local self = setmetatable({}, Pickup)
    self.itemName = config.itemName
    self.rotationSpeed = config.rotationSpeed or 100
    return self
end

function Pickup:getSource()
    return self:GetComponent("AudioSource")
end

function Pickup:Awake()
    local pos = self.gameObject:GetPivot().Position
    self.downVector = pos - Vector3.new(0, 0.5 * STUDS_PER_METER, 0)
    self.upVector = pos
    self:MoveDown()

    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        local plr = self.host.playerFromTouch(other)
        if not plr then return end
        -- other.SendMessage("GetItem", itemName)
        local playerComp = self.host.findObjectOfType("Player")
        if playerComp and playerComp.GetItem then
            playerComp:GetItem(self.itemName)
        end
        self.host.destroy(self.gameObject)
    end)
end

function Pickup:Update(dt)
    self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, math.rad(dt * self.rotationSpeed), 0))
end

function Pickup:MoveDown()
    self.host.startCoroutine(self, function()
        while self.gameObject:GetPivot().Position.Y > self.downVector.Y + 0.05 * STUDS_PER_METER do
            local dt = task.wait()
            self.gameObject:PivotTo(self.gameObject:GetPivot() + Vector3.new(0, -0.5 * STUDS_PER_METER * dt, 0))
        end
        self:MoveUp()
    end)
end

function Pickup:MoveUp()
    self.host.startCoroutine(self, function()
        while self.gameObject:GetPivot().Position.Y < self.upVector.Y - 0.05 * STUDS_PER_METER do
            local dt = task.wait()
            self.gameObject:PivotTo(self.gameObject:GetPivot() + Vector3.new(0, 0.5 * STUDS_PER_METER * dt, 0))
        end
        self:MoveDown()
    end)
end

return Pickup