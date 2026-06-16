local PlaneTexture = {}
PlaneTexture.__index = PlaneTexture

function PlaneTexture.new(config)
    local self = setmetatable({}, PlaneTexture)
    self.rotateSpeed = config.rotateSpeed or 30
    return self
end

function PlaneTexture:Update(dt)
    -- UNCONVERTED: per-frame shader material matrix animation has no Roblox equivalent.
    -- Unity rotates the propeller material's "_TextureRotation" matrix via
    -- Renderer.materials[1].SetMatrix(Matrix4x4.TRS(...)); Roblox SurfaceAppearance/Texture
    -- exposes no per-frame shader-uniform matrix API, so this animation cannot be reproduced.
end

return PlaneTexture