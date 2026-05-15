local RunService = game:GetService("RunService")

local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local rotateSpeed = 30

local startTime = os.clock()

RunService.Heartbeat:Connect(function(dt)
    local t = os.clock() - startTime
    local angle = math.rad(t * rotateSpeed)
    local rot = CFrame.Angles(0, 0, angle)
    -- UNCONVERTED: Material.SetMatrix("_TextureRotation", m) has no Roblox
    -- equivalent. Roblox materials/SurfaceAppearance do not expose shader
    -- uniform matrices to scripts. The rotation is computed above so a
    -- future implementation can apply it to a propeller MeshPart's CFrame
    -- if the propeller is split into its own Part rather than baked into
    -- a static mesh texture.
    local _ = rot
end)