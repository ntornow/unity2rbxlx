local Player = {}
Player.__index = Player

function Player:Awake()
    Player.instance = self.gameObject
    self.source = self:GetComponent("AudioSource")
    self.control = self:GetComponent("CharacterController")
    self.cam = workspace.CurrentCamera
    self.weaponSlot = self.cam and self.cam:GetChildren()[1]
end

function Player:Rotate(dt)
    local mouseDelta = self.uis:GetMouseDelta()
    local yaw = self.sensitivity * dt * mouseDelta.X
    self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))
    local x = self.camRotation.X - mouseDelta.Y * self.sensitivity * dt
    x = math.clamp(x, self.minAngle, self.maxAngle)
    self.camRotation = Vector3.new(x, 0, 0)
    local cam = workspace.CurrentCamera
    if cam then
        cam.CameraType = Enum.CameraType.Scriptable
        local basePivot = self.gameObject:GetPivot()
        cam.CFrame = CFrame.new(basePivot.Position) * (basePivot - basePivot.Position) * CFrame.Angles(-math.rad(x), 0, 0)
    end
end

function Player:Move(dt)
    local humanoid = self.control
    if not humanoid then
        return
    end
    local grounded = humanoid.FloorMaterial ~= Enum.Material.Air
    if grounded then
        local h = self:_axis(Enum.KeyCode.D, Enum.KeyCode.A)
        local v = self:_axis(Enum.KeyCode.W, Enum.KeyCode.S)
        local localDir = Vector3.new(h, 0, -v)
        self.moveDirection = self.gameObject:GetPivot():VectorToWorldSpace(localDir)
        if self:_keyDown(Enum.KeyCode.Space) then
            humanoid.Jump = true
        end
    end
    if self.moveDirection.Magnitude > 0 then
        humanoid:Move(self.moveDirection.Unit, false)
    else
        humanoid:Move(Vector3.zero, false)
    end
end

function Player:_axis(posCode, negCode)
    local a = 0
    if self.uis:IsKeyDown(posCode) then a = a + 1 end
    if self.uis:IsKeyDown(negCode) then a = a - 1 end
    return a
end

function Player:_keyDown(code)
    return self.uis:IsKeyDown(code)
end

return Player
