-- @@GAMEPLAY_RUNTIME_MODULE@@ converter-owned (Gameplay)
-- gameplay.luau: orchestrator + single entry point for per-instance
-- behaviour stubs.
--
-- Required by every emitted per-instance stub. Force-requires the
-- family modules so their ``Composer.register`` calls fire before any
-- ``run`` call. Avoids the cyclic-require problem (families require
-- Composer; Composer cannot require families; orchestrator requires
-- all of them).
--
-- Returns the Composer table directly so per-instance stubs call
-- ``Gameplay.run(container, capabilities)`` without an extra hop.

local Composer = require(script.Parent:WaitForChild("Composer"))

-- Family registration happens as a side-effect of these requires.
-- Order doesn't matter — each family registers under a different kind.
require(script.Parent:WaitForChild("Triggers"))
require(script.Parent:WaitForChild("Movement"))
require(script.Parent:WaitForChild("Lifetime"))
require(script.Parent:WaitForChild("HitDetection"))
require(script.Parent:WaitForChild("Effects"))

-- PR #73c: DamageProtocol owns the ``DamageEvent`` RemoteEvent and
-- the server-side validator. Force-requiring it here means the
-- server router is up before any adapter-driven bullet hits the
-- ``Effects.damage`` attribute mirror, and before any AI-transpiled
-- LocalScript fires the legacy-pack-injected ``FireServer`` call.
-- The module's body initializes the OnServerEvent connection on
-- first require (server-side only) and is idempotent.
--
-- PR #74 codex round-2 [P2]: the converter omits DamageProtocol
-- from ReplicatedStorage.AutoGen entirely when the project has no
-- Player-damage signal (no Effect.Damage / Effect.Splash adapter
-- match and no legacy ``FireServer(DamageEvent`` body-patch), so
-- adapter-enabled projects that use ``DamageEvent`` for unrelated
-- traffic don't get its OnServerEvent claimed. Use ``FindFirstChild``
-- (NOT ``WaitForChild``) so the orchestrator finishes loading
-- promptly in the absent case — a 5-second wait here would block
-- every door/projectile-only adapter project from registering its
-- families.
local damageProtocol = script.Parent:FindFirstChild("DamageProtocol")
if damageProtocol ~= nil then
    require(damageProtocol)
end

return Composer
