local Player = {}
Player.__index = Player

function Player:Awake()
    Player.instance = self.gameObject
    self.control = self:GetComponent("CharacterController")
    self.cam = workspace.CurrentCamera
    self.camRotationX = 0
    self.pendingMouse = Vector3.new(0, 0, 0)
end

-- The cold A-MISS shape caches GetMouseDelta in Update, then consumes the
-- cached delta in Rotate to write the camera. The split-across-Update read is
-- one of the reasons the PR #182 locators abstained on this shape.
function Player:Update(dt)
    local delta = self.uis:GetMouseDelta()
    self.pendingMouse = Vector3.new(delta.X, delta.Y, 0)
end

function Player:Rotate(dt)
    local yaw = self.sensitivity * dt * self.pendingMouse.X
    self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))
    self.camRotationX = self.camRotationX - self.pendingMouse.Y * self.sensitivity * dt
    self.camRotationX = math.clamp(self.camRotationX, self.minAngle, self.maxAngle)
    local cam = workspace.CurrentCamera
    if cam then
        cam.CameraType = Enum.CameraType.Scriptable
        local pos = self.gameObject:GetPivot().Position
        cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), 0, 0)
    end
end

function Player:Move(dt)
    local grounded = true
    if self.control and self.control:IsA("Humanoid") then
        grounded = self.control.FloorMaterial ~= Enum.Material.Air
    end
    local h = 0
    local v = 0
    if grounded then
        h = self:_getAxis(Enum.KeyCode.D, Enum.KeyCode.A)
        v = self:_getAxis(Enum.KeyCode.W, Enum.KeyCode.S)
    end
    local localDir = Vector3.new(h, 0, -v)
    local worldDir = self.gameObject:GetPivot():VectorToWorldSpace(localDir)
    local disp = worldDir * self.speed * dt * STUDS_PER_METER
    -- Rig-pivot locomotion (NOT Humanoid:Move) -- paradigm A MISSES this shape.
    self.gameObject:PivotTo(self.gameObject:GetPivot() + disp)
end

function Player:_getAxis(posCode, negCode)
    local a = 0
    if self.uis:IsKeyDown(posCode) then a = a + 1 end
    if self.uis:IsKeyDown(negCode) then a = a - 1 end
    return a
end

return Player
