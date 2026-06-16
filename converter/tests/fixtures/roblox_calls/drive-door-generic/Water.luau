local WaterMode = { Simple = 0, Reflective = 1, Refractive = 2 }

local Water = {}
Water.__index = Water

function Water.new(config)
    local self = setmetatable({}, Water)
    self.waterMode = config.waterMode or WaterMode.Refractive
    if config.disablePixelLights == nil then
        self.disablePixelLights = true
    else
        self.disablePixelLights = config.disablePixelLights
    end
    self.textureSize = config.textureSize or 256
    self.clipPlaneOffset = config.clipPlaneOffset or 0.07
    self.reflectLayers = config.reflectLayers or -1
    self.refractLayers = config.refractLayers or -1
    self.m_HardwareWaterSupport = WaterMode.Refractive
    self.m_OldReflectionTextureSize = 0
    self.m_OldRefractionTextureSize = 0
    return self
end

function Water:Awake()
    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        -- BORKS ADDON: damage the player that touches the water.
        local plr = self.host.playerFromTouch(other)
        if not plr then return end  -- ignore non-player touches
        local char = plr.Character
        if not char then return end
        local hum = char:FindFirstChildOfClass("Humanoid")
        if hum then
            hum:TakeDamage(100)
        end
    end)
end

function Water:OnWillRenderObject()
    -- UNCONVERTED: render-texture reflection/refraction cameras, oblique projection
    -- matrices, GL.invertCulling, QualitySettings.pixelLightCount, and Shader keyword
    -- toggling have no Roblox equivalent. Roblox renders water/reflections internally.
end

function Water:OnDisable()
    -- UNCONVERTED: destroys RenderTexture objects and per-camera reflection/refraction
    -- helper cameras; no Roblox equivalent (no RenderTexture, no manual scene cameras).
end

function Water:Update(dt)
    -- UNCONVERTED: writes WaveSpeed/_WaveScale/_WaveOffset/_WaveScale4 shader vectors on
    -- the renderer's sharedMaterial; Roblox materials expose no arbitrary shader-vector API.
end

function Water:UpdateCameraModes(src, dest)
    -- UNCONVERTED: copies clearFlags/skybox/clip-plane/FOV between manual scene cameras;
    -- no equivalent in Roblox's single-camera model.
end

function Water:CreateWaterObjects(currentCamera)
    -- UNCONVERTED: on-demand creation of RenderTexture + helper Camera/Skybox/FlareLayer
    -- GameObjects for reflection/refraction; no Roblox equivalent.
end

function Water:GetWaterMode()
    if self.m_HardwareWaterSupport < self.waterMode then
        return self.m_HardwareWaterSupport
    end
    return self.waterMode
end

function Water:FindHardwareWaterSupport()
    -- UNCONVERTED: probes renderer.sharedMaterial.GetTag("WATERMODE"); Roblox has no
    -- material shader-tag query, so hardware mode cannot be detected.
    return WaterMode.Simple
end

function Water:CameraSpacePlane(cam, pos, normal, sideSign)
    -- UNCONVERTED: computes a plane in camera space from worldToCameraMatrix; relies on
    -- manual camera matrices that Roblox does not expose.
end

function Water.CalculateReflectionMatrix(reflectionMat, plane)
    -- UNCONVERTED: builds a 4x4 mirror matrix for the reflection camera; unused without
    -- the manual-camera rendering path, which has no Roblox equivalent.
end

return Water