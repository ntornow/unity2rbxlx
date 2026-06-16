local CoroutineHandler = {}
CoroutineHandler.__index = CoroutineHandler

function CoroutineHandler.new(config)
    local self = setmetatable({}, CoroutineHandler)
    return self
end

function CoroutineHandler:Awake()
    -- static m_Instance = this (Unity lazily created a dedicated GameObject +
    -- DontDestroyOnLoad; in the host model the instance is constructed and bound
    -- by the host, so we just record the singleton reference here).
    CoroutineHandler.m_Instance = self
end

function CoroutineHandler.getInstance()
    -- Unity's `instance` getter lazily spawned a "CoroutineHandler" GameObject and
    -- AddComponent'd this script. There is no faithful host primitive for creating
    -- a bare GameObject, and DontDestroyOnLoad has no Roblox analog (no scene loads),
    -- so we surface whatever instance the host has bound.
    return CoroutineHandler.m_Instance
end

function CoroutineHandler:OnDisable()
    if CoroutineHandler.m_Instance then
        self.host.destroy(CoroutineHandler.m_Instance.gameObject)
    end
end

function CoroutineHandler.StartStaticCoroutine(routine)
    local inst = CoroutineHandler.getInstance()
    if inst then
        return inst.host.startCoroutine(inst, routine)
    end
    -- No bound instance available; run the routine detached.
    return task.spawn(routine)
end

return CoroutineHandler