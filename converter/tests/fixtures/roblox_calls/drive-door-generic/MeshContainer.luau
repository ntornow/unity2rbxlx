local MeshContainer = {}
MeshContainer.__index = MeshContainer

function MeshContainer.new(m)
    local self = setmetatable({}, MeshContainer)
    self.mesh = m
    self.vertices = m.vertices
    self.normals = m.normals
    return self
end

function MeshContainer:Update()
    self.mesh.vertices = self.vertices
    self.mesh.normals = self.normals
end

return MeshContainer