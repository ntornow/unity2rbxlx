local HudControl = {}
HudControl.__index = HudControl

local Player = require(game:GetService("ReplicatedStorage"):FindFirstChild("Player", true) or game:GetService("ServerStorage"):FindFirstChild("Player", true))

-- Idempotent shared-BindableEvent factory. Mirrors Player's own static-event
-- creation so HudControl and Player converge on the SAME module-table field
-- regardless of Awake ordering (no duplicate BindableEvents in ReplicatedStorage).
local function ensureBindable(name)
    local rs = game:GetService("ReplicatedStorage")
    local existing = rs:FindFirstChild(name)
    if existing then return existing end
    local b = Instance.new("BindableEvent")
    b.Name = name
    b.Parent = rs
    return b
end

function HudControl.new(config)
    local self = setmetatable({}, HudControl)
    config = config or {}
    -- No scalar config; HUD references resolve lazily from self.gameObject in Awake onward.
    return self
end

-- Reference getters: Unity's properties re-Find each access, so these re-resolve
-- the child each call (the HUD subtree is stable but cheap to look up).

-- private Transform module { get { return transform.Find("Module"); } }
function HudControl:_module()
    return self.gameObject:FindFirstChild("Module")
end

-- private Transform itemModule { get { return transform.Find("ItemModule"); } }
function HudControl:_itemModule()
    return self.gameObject:FindFirstChild("ItemModule")
end

-- private Transform ammo { get { return module.Find("Ammo"); } }
function HudControl:_ammo()
    local m = self:_module()
    return m and m:FindFirstChild("Ammo")
end

-- private GameObject pauseMenu { get { return transform.Find("Pause").gameObject; } }
function HudControl:_pauseMenu()
    return self.gameObject:FindFirstChild("Pause")
end

-- private Text ammoCur { get { return ammo.Find("Cur").GetComponent<Text>(); } }
function HudControl:_ammoCur()
    local a = self:_ammo()
    return a and a:FindFirstChild("Cur")
end

-- private Slider health { get { return module.Find("Health").GetComponent<Slider>(); } }
function HudControl:_health()
    local m = self:_module()
    return m and m:FindFirstChild("Health")
end

-- Best-effort Text.text writer: Unity Text → Roblox TextLabel/Button/Box; if the
-- found object is a container, drive the first text descendant.
function HudControl:_setText(obj, str)
    if not obj then return end
    if obj:IsA("TextLabel") or obj:IsA("TextButton") or obj:IsA("TextBox") then
        obj.Text = str
        return
    end
    for _, d in obj:GetDescendants() do
        if d:IsA("TextLabel") or d:IsA("TextButton") or d:IsA("TextBox") then
            d.Text = str
            return
        end
    end
end

function HudControl:Awake()
    -- ammo.Find("Total").GetComponent<Text>().text = Player.maxAmmo.ToString();
    local ammo = self:_ammo()
    if ammo then
        local total = ammo:FindFirstChild("Total")
        if total then self:_setText(total, tostring(Player.maxAmmo)) end
    end

    -- Player.HealthUpdate += UpdatePlayerHealth; (and Ammo/Item/Pause).
    -- Connect to the SAME shared field Player fires on (idempotent create) so
    -- ordering between HudControl.Awake and Player.Awake is irrelevant.
    Player.HealthUpdate = Player.HealthUpdate or ensureBindable("HealthUpdate")
    Player.AmmoUpdate = Player.AmmoUpdate or ensureBindable("AmmoUpdate")
    Player.ItemUpdate = Player.ItemUpdate or ensureBindable("ItemUpdate")
    Player.PauseEvent = Player.PauseEvent or ensureBindable("PauseEvent")

    Player.HealthUpdate.Event:Connect(function(curHealth) self:UpdatePlayerHealth(curHealth) end)
    Player.AmmoUpdate.Event:Connect(function(curAmmo) self:UpdatePlayerAmmo(curAmmo) end)
    Player.ItemUpdate.Event:Connect(function(itemName) self:UpdatePlayerItems(itemName) end)
    Player.PauseEvent.Event:Connect(function(pause) self:PauseMenu(pause) end)
end

function HudControl:PauseMenu(pause)
    -- Time.timeScale = pause ? 0 : 1. Roblox has no global time-scale freeze; record
    -- the intended scale on workspace so any reader can observe the paused state.
    workspace:SetAttribute("TimeScale", pause and 0 or 1)

    -- muted/normal are AudioMixerSnapshot serialized fields; AudioMixerSnapshot has no
    -- Roblox equivalent, so muted.TransitionTo(0) / normal.TransitionTo(0) is inert.
    -- UNCONVERTED: AudioMixerSnapshot.TransitionTo

    -- pauseMenu.SetActive(pause): the pause UI is a GuiObject, so toggle Visible.
    local pm = self:_pauseMenu()
    if pm then
        if pm:IsA("GuiObject") then
            pm.Visible = pause
        else
            self.host.setActive(pm, pause)
        end
    end
end

function HudControl:UpdatePlayerItems(itemName)
    -- itemModule.Find(itemName).GetComponent<Toggle>().isOn = true;
    local im = self:_itemModule()
    if not im then return end
    local item = im:FindFirstChild(itemName)
    if not item then return end
    -- Roblox has no Toggle; record isOn and best-effort reveal the collected item.
    item:SetAttribute("isOn", true)
    if item:IsA("GuiObject") then item.Visible = true end
end

function HudControl:UpdatePlayerAmmo(curAmmo)
    -- ammoCur.text = curAmmo.ToString();
    self:_setText(self:_ammoCur(), tostring(curAmmo))
end

function HudControl:UpdatePlayerHealth(curHealth)
    -- float percentage = (float)curHealth / Player.maxHealth; health.value = percentage;
    local percentage = curHealth / Player.maxHealth
    local h = self:_health()
    if h then
        -- Roblox has no Slider; record the normalized value and, best-effort, drive a
        -- child "Fill" bar's horizontal scale.
        h:SetAttribute("Value", percentage)
        local fill = h:FindFirstChild("Fill")
        if fill and fill:IsA("GuiObject") then
            fill.Size = UDim2.new(percentage, 0, fill.Size.Y.Scale, fill.Size.Y.Offset)
        end
    end
end

return HudControl