local PlanarReflection = {}
PlanarReflection.__index = PlanarReflection

function PlanarReflection.new(config)
    local self = setmetatable({}, PlanarReflection)
    self.reflectionMask = config.reflectionMask
    self.reflectSkybox = config.reflectSkybox or false
    self.clearColor = config.clearColor or Color3.fromRGB(128, 128, 128)
    self.reflectionSampler = config.reflectionSampler or "_ReflectionTex"
    self.clipPlaneOffset = config.clipPlaneOffset or 0.07
    self.m_Oldpos = Vector3.new(0, 0, 0)
    self.m_ReflectionCamera = nil
    self.m_SharedMaterial = nil
    self.m_HelperCameras = nil
    return self
end

function PlanarReflection:Start()
    -- UNCONVERTED: fetches the WaterBase.sharedMaterial shader material; Roblox exposes
    -- no per-renderer shader material, so there is nothing to bind the reflection texture to.
end

function PlanarReflection:CreateReflectionCameraFor(cam)
    -- UNCONVERTED: creates a helper Camera GameObject + RenderTexture target for off-screen
    -- reflection rendering; Roblox has no manual scene cameras or RenderTexture.
end

function PlanarReflection:SetStandardCameraParameter(cam, mask)
    -- UNCONVERTED: sets cullingMask / backgroundColor / enabled on a manual helper camera;
    -- no Roblox equivalent (single built-in camera, no culling-mask layer model).
end

function PlanarReflection:CreateTextureFor(cam)
    -- UNCONVERTED: allocates a RenderTexture sized to half the camera's pixel dimensions;
    -- Roblox has no RenderTexture type.
end

function PlanarReflection:RenderHelpCameras(currentCam)
    -- UNCONVERTED: lazily builds + renders the per-camera reflection helper camera; depends on
    -- the manual-camera render path that has no Roblox equivalent.
end

function PlanarReflection:LateUpdate(dt)
    if self.m_HelperCameras ~= nil then
        table.clear(self.m_HelperCameras)
    end
end

function PlanarReflection:WaterTileBeingRendered(tr, currentCam)
    -- UNCONVERTED: drives RenderHelpCameras and writes the reflection RenderTexture onto the
    -- shared shader material; neither manual cameras nor shader-texture binding exist in Roblox.
end

function PlanarReflection:OnEnable()
    -- UNCONVERTED: toggles the WATER_REFLECTIVE / WATER_SIMPLE shader keywords; Roblox materials
    -- expose no shader-keyword API.
end

function PlanarReflection:OnDisable()
    -- UNCONVERTED: toggles the WATER_SIMPLE / WATER_REFLECTIVE shader keywords; Roblox materials
    -- expose no shader-keyword API.
end

function PlanarReflection:RenderReflectionFor(cam, reflectCamera)
    -- UNCONVERTED: the core reflection pass — mirror matrix, oblique near-plane clipping,
    -- worldToCameraMatrix / projectionMatrix overrides, GL.invertCulling, manual Camera.Render();
    -- none of these manual-camera matrix operations have a Roblox equivalent.
end

function PlanarReflection:SaneCameraSettings(helperCam)
    -- UNCONVERTED: configures depthTextureMode / clearFlags / renderingPath on a manual helper
    -- camera; no Roblox equivalent.
end

function PlanarReflection.CalculateObliqueMatrix(projection, clipPlane)
    -- UNCONVERTED: builds an oblique near-plane-clipped projection matrix for the reflection
    -- camera; unused without the manual-camera rendering path, which Roblox does not expose.
end

function PlanarReflection.CalculateReflectionMatrix(reflectionMat, plane)
    -- UNCONVERTED: builds a 4x4 mirror matrix for the reflection camera; unused without the
    -- manual-camera rendering path, which has no Roblox equivalent.
end

function PlanarReflection.Sgn(a)
    if a > 0.0 then
        return 1.0
    end
    if a < 0.0 then
        return -1.0
    end
    return 0.0
end

function PlanarReflection:CameraSpacePlane(cam, pos, normal, sideSign)
    -- UNCONVERTED: transforms a world-space plane into camera space via worldToCameraMatrix;
    -- relies on manual camera matrices that Roblox does not expose.
end

return PlanarReflection