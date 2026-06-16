local Machine = {}
Machine.__index = Machine

local STUDS_PER_METER = 3.571

function Machine.new(config)
    local self = setmetatable({}, Machine)
    config = config or {}
    -- Scalars / plain state only. Serialized references (itemPrefabs, doors,
    -- planeHolder) are injected on self by the host before Awake.
    self.activated = 0
    self.items = {}
    self.itemNames = {}
    self.itemPlaced = {false, false, false}
    return self
end

function Machine:Awake()
    -- items[i] = transform.Find("Item " + (i + 1)); itemNames[i] = itemPrefabs[i].name.
    for i = 0, 3 do
        self.items[i + 1] = self.gameObject:FindFirstChild("Item " .. (i + 1))
        if i < 3 then
            -- Serialized prefab fields are injected as opaque prefab_ids; the host
            -- supplies a readable name when the handle carries one.
            local prefab = self.itemPrefabs and self.itemPrefabs[i + 1]
            if prefab then
                self.itemNames[i + 1] = prefab.name
            end
        end
    end

    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        -- other.tag == "Player": resolve the owning Player from the raw touch part.
        local player = self.host.playerFromTouch(other)
        if not player then return end
        -- player.hasItems.Contains(itemNames[i]) -> read the canonical "has<Item>"
        -- Attribute the Player script mirrors on pickup (cross-script contract).
        for i = 0, 2 do
            local nm = self.itemNames[i + 1]
            if nm then
                local flag = "has" .. (nm:gsub("[^%w_]+", "_"))
                if player:GetAttribute(flag) then
                    self:PlaceItem(i)
                end
            end
        end
    end)
end

function Machine:PlaceItem(number)
    if not self.itemPlaced[number + 1] then
        -- Instantiate(itemPrefabs[number], items[activated].position, items[activated].rotation),
        -- then SetParent(items[activated++]) -- both use the current activated, then increment.
        local slot = self.items[self.activated + 1]
        local cf = (slot and slot:GetPivot()) or CFrame.new()
        local item = self.host.instantiatePrefab(self.itemPrefabs[number + 1], slot, cf)
        if item and slot then
            item.Parent = slot
        end
        self.activated = self.activated + 1
        self.itemPlaced[number + 1] = true

        if number < 2 then
            -- doors[number].SendMessage("ToggleDoor", true, DontRequireReceiver):
            -- signal the Door via the canonical cross-script Attribute channel.
            local door = self.doors and self.doors[number + 1]
            if door then
                door:SetAttribute("ToggleDoor", true)
            end
        end

        -- planeHolder.SetInteger("actionNumber", activated): inert Animator param mirror.
        if self.planeHolder then
            self.planeHolder:SetAttribute("actionNumber", self.activated)
        end

        if self.activated == 3 then
            -- Instantiate(itemPrefabs[activated], items[activated].position, Quaternion.identity).
            local finalSlot = self.items[self.activated + 1]
            local pos = (finalSlot and finalSlot:GetPivot().Position) or Vector3.new()
            self.host.instantiatePrefab(self.itemPrefabs[self.activated + 1], workspace, CFrame.new(pos))
        end
    end
end

return Machine