-- Services
local RunService = game:GetService("RunService")

-- Container dispatch
local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local function getPosition() return getCFrame().Position end
local function getUp() return getCFrame().UpVector end

local PlanarReflection = {}
-- Guard: skip runtime code if script is in ReplicatedStorage
if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model") or script.Parent:IsA("Folder")) then
    return PlanarReflection
end


-- Configuration (from serialized fields)
local reflectionMask = 0
local reflectSkybox = false
local clearColor = Color3.fromRGB(128, 128, 128)
local reflectionSampler = "_ReflectionTex"
local clipPlaneOffset = 0.07

-- State
local m_Oldpos = Vector3.new(0, 0, 0)
local m_ReflectionCamera = nil
local m_SharedMaterial = nil
local m_HelperCameras = nil

local function Start()
    -- UNCONVERTED: WaterBase.sharedMaterial reference. Roblox does not expose
    -- shared materials between parts the way Unity does. The water texture
    -- update is handled by the Water script directly on its part.
    m_SharedMaterial = nil
end

local function SaneCameraSettings(helperCam)
    -- UNCONVERTED: Camera.depthTextureMode / clearFlags / renderingPath have
    -- no Roblox equivalent.
end

local function Sgn(a)
    if a > 0.0 then
        return 1.0
    end
    if a < 0.0 then
        return -1.0
    end
    return 0.0
end

local function CalculateObliqueMatrix(projection, clipPlane)
    -- UNCONVERTED: Matrix4x4 inverse and projection matrix manipulation are
    -- not exposed in Roblox. Returning the projection unchanged.
    return projection
end

local function CalculateReflectionMatrix(reflectionMat, plane)
    reflectionMat[1] = {
        1.0 - 2.0 * plane[1] * plane[1],
        -2.0 * plane[1] * plane[2],
        -2.0 * plane[1] * plane[3],
        -2.0 * plane[4] * plane[1],
    }
    reflectionMat[2] = {
        -2.0 * plane[2] * plane[1],
        1.0 - 2.0 * plane[2] * plane[2],
        -2.0 * plane[2] * plane[3],
        -2.0 * plane[4] * plane[2],
    }
    reflectionMat[3] = {
        -2.0 * plane[3] * plane[1],
        -2.0 * plane[3] * plane[2],
        1.0 - 2.0 * plane[3] * plane[3],
        -2.0 * plane[4] * plane[3],
    }
    reflectionMat[4] = {0.0, 0.0, 0.0, 1.0}
    return reflectionMat
end

local function CameraSpacePlane(cam, pos, normal, sideSign)
    -- UNCONVERTED: Camera.worldToCameraMatrix and MultiplyPoint/MultiplyVector
    -- have no Roblox equivalent. Returning a zeroed plane.
    return {0, 0, 0, 0}
end

local function CreateTextureFor(cam)
    -- UNCONVERTED: RenderTexture creation is not supported in Roblox.
    return nil
end

local function SetStandardCameraParameter(cam, mask)
    -- UNCONVERTED: Camera.cullingMask / LayerMask have no Roblox equivalent.
end

local function CreateReflectionCameraFor(cam)
    -- UNCONVERTED: Dynamically created secondary Camera objects with
    -- backgroundColor / clearFlags / targetTexture are not supported in
    -- Roblox. Only workspace.CurrentCamera exists.
    return nil
end

local function RenderReflectionFor(cam, reflectCamera)
    if not reflectCamera then
        return
    end

    if m_SharedMaterial then
        -- UNCONVERTED: Material.HasProperty check has no Roblox equivalent.
    end

    SaneCameraSettings(reflectCamera)

    local pos = getPosition()
    local normal = getUp()
    local d = -normal:Dot(pos) - clipPlaneOffset
    local reflectionPlane = {normal.X, normal.Y, normal.Z, d}

    local reflection = {{0,0,0,0},{0,0,0,0},{0,0,0,0},{0,0,0,0}}
    reflection = CalculateReflectionMatrix(reflection, reflectionPlane)

    if cam then
        m_Oldpos = cam.CFrame.Position
    end

    -- UNCONVERTED: reflection camera transform / projection matrix override
    -- and Render() call are not possible in Roblox; only the primary camera
    -- exists and its matrix is engine-managed.
    local clipPlane = CameraSpacePlane(reflectCamera, pos, normal, 1.0)
    local projection = CalculateObliqueMatrix({{0,0,0,0},{0,0,0,0},{0,0,0,0},{0,0,0,0}}, clipPlane)
end

local function RenderHelpCameras(currentCam)
    if m_HelperCameras == nil then
        m_HelperCameras = {}
    end

    if m_HelperCameras[currentCam] == nil then
        m_HelperCameras[currentCam] = false
    end
    if m_HelperCameras[currentCam] then
        return
    end

    if not m_ReflectionCamera then
        m_ReflectionCamera = CreateReflectionCameraFor(currentCam)
    end

    RenderReflectionFor(currentCam, m_ReflectionCamera)

    m_HelperCameras[currentCam] = true
end

local function LateUpdate()
    if m_HelperCameras ~= nil then
        m_HelperCameras = {}
    end
end

local function WaterTileBeingRendered(tr, currentCam)
    RenderHelpCameras(currentCam)
    -- UNCONVERTED: Material.SetTexture for the reflection sampler is not
    -- supported in Roblox (no equivalent shader uniform binding).
end

local function OnEnable()
    -- UNCONVERTED: Shader.EnableKeyword / DisableKeyword has no Roblox
    -- equivalent. Roblox materials do not expose shader variant keywords.
end

local function OnDisable()
    -- UNCONVERTED: Shader.EnableKeyword / DisableKeyword has no Roblox
    -- equivalent.
end

PlanarReflection.RenderHelpCameras = RenderHelpCameras
PlanarReflection.WaterTileBeingRendered = WaterTileBeingRendered
PlanarReflection.OnEnable = OnEnable
PlanarReflection.OnDisable = OnDisable

-- Lifecycle: Start runs at top-level, LateUpdate via Heartbeat (Roblox has
-- no separate LateUpdate event; Heartbeat fires after physics each frame).
Start()
OnEnable()

RunService.Heartbeat:Connect(function(dt)
    LateUpdate()
end)

script.Destroying:Connect(OnDisable)

return PlanarReflection