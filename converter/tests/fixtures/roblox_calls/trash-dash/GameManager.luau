local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

-- abstract class AState : MonoBehaviour has no runtime body — it only declares
-- the Enter/Exit/Tick/GetName contract its concrete subclasses (GameState,
-- GameOverState, LoadoutState, ...) implement as their own ModuleScripts. The
-- `manager` field is injected on each state by GameManager:OnEnable below. No
-- Luau base table is emitted (this module returns a single class table).

local GameManager = {}
GameManager.__index = GameManager

-- static GameManager instance { get { return s_Instance; } } -> read as
-- GameManager.instance by other scripts; assigned in OnEnable.
GameManager.instance = nil

function GameManager.new(config)
	local self = setmetatable({}, GameManager)
	-- Pure constructor: C# field initializers only. The serialized reference
	-- fields `states` (AState[]) and `m_ConsumableDatabase` are injected on self
	-- by the host before OnEnable; never read here.
	self.m_StateStack = {} -- new List<AState>()
	self.m_StateDict = {} -- new Dictionary<string, AState>()
	return self
end

-- public AState topState { get { if (m_StateStack.Count == 0) return null; ... } }
function GameManager:getTopState()
	if #self.m_StateStack == 0 then
		return nil
	end
	return self.m_StateStack[#self.m_StateStack]
end

function GameManager:OnEnable()
	PlayerData.Create()

	GameManager.instance = self -- s_Instance = this

	self.m_ConsumableDatabase:Load()

	-- We build a dictionnary from state for easy switching using their name.
	table.clear(self.m_StateDict)

	if #self.states == 0 then
		return
	end

	for i = 1, #self.states do
		self.states[i].manager = self
		self.m_StateDict[self.states[i]:GetName()] = self.states[i]
	end

	table.clear(self.m_StateStack)

	self:PushState(self.states[1]:GetName())
end

function GameManager:Update(dt)
	if #self.m_StateStack > 0 then
		self.m_StateStack[#self.m_StateStack]:Tick()
	end
end

function GameManager:OnApplicationQuit()
	-- UNCONVERTED: Unity OnApplicationQuit + UnityEngine.Analytics CustomEvent
	-- ("user_end_session") has no Roblox host event or analytics equivalent. Body
	-- was only compiled under UNITY_ANALYTICS, which is not defined here.
end

-- State management
function GameManager:SwitchState(newState)
	local state = self:FindState(newState)
	if state == nil then
		warn("Can't find the state named " .. newState)
		return
	end

	self.m_StateStack[#self.m_StateStack]:Exit(state)
	state:Enter(self.m_StateStack[#self.m_StateStack])
	table.remove(self.m_StateStack, #self.m_StateStack)
	table.insert(self.m_StateStack, state)
end

function GameManager:FindState(stateName)
	local state = self.m_StateDict[stateName]
	if state == nil then
		return nil
	end

	return state
end

function GameManager:PopState()
	if #self.m_StateStack < 2 then
		warn("Can't pop states, only one in stack.")
		return
	end

	self.m_StateStack[#self.m_StateStack]:Exit(self.m_StateStack[#self.m_StateStack - 1])
	self.m_StateStack[#self.m_StateStack - 1]:Enter(self.m_StateStack[#self.m_StateStack - 1])
	table.remove(self.m_StateStack, #self.m_StateStack)
end

function GameManager:PushState(name)
	local state = self.m_StateDict[name]
	if state == nil then
		warn("Can't find the state named " .. name)
		return
	end

	if #self.m_StateStack > 0 then
		self.m_StateStack[#self.m_StateStack]:Exit(state)
		state:Enter(self.m_StateStack[#self.m_StateStack])
	else
		state:Enter(nil)
	end

	table.insert(self.m_StateStack, state)
end

return GameManager