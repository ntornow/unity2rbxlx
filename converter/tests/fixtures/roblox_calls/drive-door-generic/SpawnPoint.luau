local SpawnPoint = {}
SpawnPoint.__index = SpawnPoint

function SpawnPoint.new(config)
    local self = setmetatable({}, SpawnPoint)
    -- No scalar config; activateOnSpawn is a GameObject reference injected on self before Awake.
    return self
end

function SpawnPoint:Awake()
    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        local plr = self.host.playerFromTouch(other)
        if not plr then return end  -- only react to player touches (Unity: other.tag == "Player")
        -- other.SendMessage("UpdateSpawnpoint", transform): dispatch to the Player component.
        local player = self.host.findObjectOfType("Player")
        if player and player.UpdateSpawnpoint then
            player:UpdateSpawnpoint(self.transform)
        end
        if self.activateOnSpawn ~= nil then
            self.host.setActive(self.activateOnSpawn, true)
        end
        self.host.destroy(self)
    end)
end

return SpawnPoint