local Helpers = {}

-- UNCONVERTED: Unity render layers / culling masks have no Roblox equivalent.
-- This walked all Renderer children and reassigned their GameObject.layer so an
-- object could be drawn by a single camera (e.g. the loadout UI camera). Roblox
-- has no per-object camera layer system, so the recursion is preserved but the
-- per-renderer assignment is inert.
function Helpers.SetRendererLayerRecursive(root, layer)
	if root == nil then return end
	for _, descendant in ipairs(root:GetDescendants()) do
		if descendant:IsA("BasePart") or descendant:IsA("Decal") or descendant:IsA("Texture") then
			-- [Unity-only] descendant.gameObject.layer = layer
		end
	end
end

return Helpers