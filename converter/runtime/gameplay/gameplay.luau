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

return Composer
