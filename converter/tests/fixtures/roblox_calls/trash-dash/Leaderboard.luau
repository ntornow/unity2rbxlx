local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local Leaderboard = {}
Leaderboard.__index = Leaderboard

function Leaderboard.new(config)
    local self = setmetatable({}, Leaderboard)
    -- Scalars only. entriesRoot (RectTransform) and playerEntry (HighscoreUI)
    -- are reference fields injected on self by the host before Awake.
    self.entriesCount = config.entriesCount or 0
    self.forcePlayerDisplay = config.forcePlayerDisplay or false
    self.displayPlayer = config.displayPlayer
    if self.displayPlayer == nil then
        self.displayPlayer = true
    end
    return self
end

function Leaderboard:Open()
    self.host.setActive(self.gameObject, true)
    self:Populate()
end

function Leaderboard:Close()
    self.host.setActive(self.gameObject, false)
end

function Leaderboard:Populate()
    -- entriesRoot children are the entry frames. Unity sibling-index ordering
    -- (SetAsLastSibling / SetSiblingIndex) maps to LayoutOrder, which drives
    -- Roblox UIListLayout/UIGridLayout ordering (Roblox has no sibling index).
    local children = self.entriesRoot:GetChildren()

    -- playerEntry.transform.SetAsLastSibling() -> highest LayoutOrder.
    self.playerEntry.gameObject.LayoutOrder = #children

    -- Enable all entries.
    for i = 0, self.entriesCount - 1 do
        local child = children[i + 1]
        if child then
            self.host.setActive(child, true)
        end
    end

    -- Find all index in local page space.
    local localStart = 0
    local place = -1
    local localPlace = -1

    if self.displayPlayer then
        place = PlayerData.instance:GetScorePlace(tonumber(self.playerEntry.score.Text))
        localPlace = place - localStart
    end

    if localPlace >= 0 and localPlace < self.entriesCount and self.displayPlayer then
        self.host.setActive(self.playerEntry.gameObject, true)
        -- playerEntry.transform.SetSiblingIndex(localPlace) -> LayoutOrder.
        self.playerEntry.gameObject.LayoutOrder = localPlace
    end

    if (not self.forcePlayerDisplay) or #PlayerData.instance.highscores < self.entriesCount then
        -- entriesRoot.GetChild(childCount - 1) -> last child.
        local last = children[#children]
        if last then
            self.host.setActive(last, false)
        end
    end

    local currentHighScore = localStart

    for i = 0, self.entriesCount - 1 do
        local frame = children[i + 1]

        -- entriesRoot.GetChild(i).GetComponent<HighscoreUI>(): the host registry
        -- isn't keyed for component lookup on an arbitrary child Instance, so the
        -- HighscoreUI is resolved from the frame's named Text children. Identity
        -- against the player entry is compared at the frame (gameObject) level.
        if frame == nil or frame == self.playerEntry.gameObject then
            -- We skip the player entry (and any missing child).
            continue
        end

        local playerNameLabel = frame:FindFirstChild("playerName")
        local numberLabel = frame:FindFirstChild("number")
        local scoreLabel = frame:FindFirstChild("score")

        if scoreLabel == nil then
            -- GetComponent<HighscoreUI>() returned null on this child.
            continue
        end

        if #PlayerData.instance.highscores > currentHighScore then
            self.host.setActive(frame, true)
            playerNameLabel.Text = PlayerData.instance.highscores[currentHighScore + 1].name
            numberLabel.Text = tostring(localStart + i + 1)
            scoreLabel.Text = tostring(PlayerData.instance.highscores[currentHighScore + 1].score)

            currentHighScore = currentHighScore + 1
        else
            self.host.setActive(frame, false)
        end
    end

    -- If we force the player to be displayed, enable it even if disabled elsewhere.
    if self.forcePlayerDisplay then
        self.host.setActive(self.playerEntry.gameObject, true)
    end

    self.playerEntry.number.Text = tostring(place + 1)
end

return Leaderboard