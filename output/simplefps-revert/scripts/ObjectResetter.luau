local container = script.Parent

if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local function setCFrame(cf)
    if container:IsA("Model") then container:PivotTo(cf) else container.CFrame = cf end
end

local function getRigidbody()
    if container:IsA("BasePart") then return container end
    if container:IsA("Model") then
        return container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    end
    return nil
end

local originalCFrame = getCFrame()
local originalStructure = {}
for _, c in ipairs(container:GetDescendants()) do
    originalStructure[c] = true
end
originalStructure[container] = true

local rigidbody = getRigidbody()

local ObjectResetter = {}
-- Guard: skip runtime code if script is in ReplicatedStorage
if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model") or script.Parent:IsA("Folder")) then
    return ObjectResetter
end


function ObjectResetter.ResetCoroutine(delay)
    task.wait(delay)

    for _, t in ipairs(container:GetDescendants()) do
        if not originalStructure[t] then
            t.Parent = nil
        end
    end

    setCFrame(originalCFrame)

    if rigidbody then
        rigidbody.AssemblyLinearVelocity = Vector3.zero
        rigidbody.AssemblyAngularVelocity = Vector3.zero
    end

    container:SetAttribute("Reset", true)
end

function ObjectResetter.DelayedReset(delay)
    task.spawn(ObjectResetter.ResetCoroutine, delay)
end

container:GetAttributeChangedSignal("DelayedReset"):Connect(function()
    local delay = container:GetAttribute("DelayedReset")
    if typeof(delay) == "number" then
        ObjectResetter.DelayedReset(delay)
    end
end)

return ObjectResetter