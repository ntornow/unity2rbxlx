local Door = {}
Door.__index = Door

function Door.new(config)
    local self = setmetatable({}, Door)
    config = config or {}
    -- Scalars only; never touch self.host / GetComponent here.
    self.closeTime = config.closeTime or 5
    return self
end

function Door:Awake()
    -- OnTriggerEnter(other)
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        -- if (other.tag == "Player")
        local plr = self.host.playerFromTouch(other)
        if not plr then return end  -- ignore non-player touches
        if self:_hasKey(plr) then
            self:ToggleDoor(true)
        end
    end)

    -- OnTriggerExit(other)
    self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", function(other)
        -- if (other.tag == "Player")
        local plr = self.host.playerFromTouch(other)
        if not plr then return end  -- ignore non-player touches
        if self:_hasKey(plr) then
            self:ToggleDoor(false)
        end
    end)
end

-- other.GetComponent<Player>().hasKey — read the canonical cross-script Attribute
-- mirror (Player writes "hasKey" on the Player Instance + Character Model), not the
-- Player module's private field.
function Door:_hasKey(plr)
    local v = plr:GetAttribute("hasKey")
    if not v and plr.Character then
        v = plr.Character:GetAttribute("hasKey")
    end
    return v
end

-- private AudioSource source { get { return GetComponent<AudioSource>(); } }
function Door:_getSource()
    return self:GetComponent("AudioSource")
end

-- private Animator doorAnim { get { return transform.parent.Find("door").GetComponent<Animator>(); } }
function Door:_getDoorAnim()
    local parent = self.gameObject.Parent
    return parent and parent:FindFirstChild("door")
end

function Door:ToggleDoor(value)
    -- doorAnim.SetBool("open", value) — Animator params degrade to attributes (no
    -- skeletal pipeline); record on the "door" sibling object.
    local doorObj = self:_getDoorAnim()
    if doorObj then
        doorObj:SetAttribute("open", value)
    end

    local source = self:_getSource()
    if source then
        -- source.pitch = Random.Range(0.8f, 1.2f)
        source.PlaybackSpeed = math.random() * 0.4 + 0.8
        source:Play()
    end
end

return Door