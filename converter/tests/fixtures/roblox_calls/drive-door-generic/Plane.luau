local Plane = {}
Plane.__index = Plane

function Plane.new(config)
    local self = setmetatable({}, Plane)
    -- No serialized scalars; flyingPlane (prefab_id) and cutsceneCamera are
    -- injected on self before Awake. Never read them here.
    return self
end

function Plane:Awake()
    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        -- other.CompareTag("Player") + other.GetComponent<Player>()
        local plr = self.host.playerFromTouch(other)
        if not plr then return end

        -- player.hasItems.Exists(x => x.Equals("GasCan")): the canonical cross-script
        -- read is the "hasGasCan" Attribute mirror that Player:GetItem writes.
        local char = plr.Character
        local hasGasCan = plr:GetAttribute("hasGasCan") or (char and char:GetAttribute("hasGasCan"))
        if not hasGasCan then return end

        -- GameObject plane = Instantiate(flyingPlane); plane.transform.SetParent(transform.parent)
        if self.flyingPlane then
            self.host.instantiatePrefab(self.flyingPlane, self.gameObject.Parent, self.gameObject:GetPivot())
        end

        -- FindAnyObjectByType<GameManager>().SendMessage("RestartGame", 5)
        local gm = self.host.findObjectOfType("GameManager")
        if gm then gm:RestartGame(5) end

        -- Destroy(player.gameObject)
        if char then self.host.destroy(char) end

        -- cutsceneCamera.GetComponent<Camera>().enabled = true
        -- cutsceneCamera.GetComponent<AudioListener>().enabled = true
        -- UNCONVERTED: Unity multi-camera/AudioListener enable has no Roblox analog —
        -- Roblox has a single workspace.CurrentCamera and no per-object AudioListener
        -- toggle; a cutscene-camera switch is a deploy-time decision, not a faithful
        -- in-engine translation.

        -- Destroy(gameObject)
        self.host.destroy(self.gameObject)
    end)
end

return Plane