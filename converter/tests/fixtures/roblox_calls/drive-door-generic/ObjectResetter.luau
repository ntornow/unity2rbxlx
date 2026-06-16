local Class = {}
Class.__index = Class

local STUDS_PER_METER = 3.571

function Class.new(config)
    local self = setmetatable({}, Class)
    self.originalStructure = nil
    self.originalPosition = nil
    self.originalRotation = nil
    self.Rigidbody = nil
    return self
end

function Class:Start()
    -- originalStructure = new List<Transform>(GetComponentsInChildren<Transform>())
    self.originalStructure = {}
    table.insert(self.originalStructure, self.gameObject)
    for _, d in self.gameObject:GetDescendants() do
        table.insert(self.originalStructure, d)
    end

    local pivot = self.gameObject:GetPivot()
    self.originalPosition = pivot.Position
    -- rotation-only CFrame (orientation at the origin)
    self.originalRotation = pivot.Rotation

    self.Rigidbody = self:GetComponent("Rigidbody")
end

function Class:DelayedReset(delay)
    self.host.startCoroutine(self, function()
        self:ResetCoroutine(delay)
    end)
end

function Class:ResetCoroutine(delay)
    task.wait(delay)

    -- remove any gameobjects added (fire, skid trails, etc)
    for _, t in self.gameObject:GetDescendants() do
        if not table.find(self.originalStructure, t) then
            t.Parent = nil
        end
    end

    self.gameObject:PivotTo(CFrame.new(self.originalPosition) * self.originalRotation)

    if self.Rigidbody then
        self.Rigidbody.AssemblyLinearVelocity = Vector3.zero
        self.Rigidbody.AssemblyAngularVelocity = Vector3.zero
    end

    -- SendMessage("Reset"): Unity broadcasts to every component on this GameObject.
    -- Roblox has no name-dispatch; best-effort invoke "Reset" on this instance.
    if type(self.Reset) == "function" then
        self:Reset()
    end
end

return Class