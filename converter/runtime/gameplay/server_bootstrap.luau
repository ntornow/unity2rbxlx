-- server_bootstrap.luau (parented to ServerScriptService at emit time).
--
-- Force-loads the Gameplay orchestrator on server start so the
-- damage-routing protocol (and any family runtime that needs
-- server-side init) is ready BEFORE any client fires a RemoteEvent.
--
-- Why a separate Script (not a ModuleScript): all the family modules
-- live under ReplicatedStorage.AutoGen, but ReplicatedStorage scripts
-- don't auto-run; they only execute when something else requires them.
-- Per-instance adapter stubs that DO require Gameplay are typically
-- attached to prefab templates in ReplicatedStorage.Templates — those
-- stubs don't fire until the first runtime clone spawns into Workspace.
--
-- That means a Player LocalScript that fires ``DamageEvent`` on its
-- first click (before any bullet has spawned) would hit a nil
-- RemoteEvent. The legacy ``_AutoDamageEventRouter`` was an always-on
-- ServerScriptService Script for exactly this reason; PR #73c moved
-- the router logic into a ModuleScript, so we need a thin bootstrap
-- Script to keep the always-on posture.
--
-- The require is a no-op if Gameplay was already loaded by some other
-- code path (e.g. a workspace-placed adapter stub running on init).
-- DamageProtocol's ``_initServer`` is itself idempotent so a
-- double-load can't double-bind ``OnServerEvent``.
--
-- Codex PR #73c-round-1 [P1]: without this bootstrap, prefab-only
-- adapter coverage (the common SimpleFPS shape — TurretBullet /
-- PlaneBullet / SciFi_Door all live on prefab templates) silently
-- drops every early Player shot.

local ReplicatedStorage = game:GetService("ReplicatedStorage")

local autoGen = ReplicatedStorage:WaitForChild("AutoGen", 30)
if autoGen == nil then
    warn(
        "[gameplay/server_bootstrap] ReplicatedStorage.AutoGen missing — "
        .. "Gameplay orchestrator can't load. Adapter-driven damage "
        .. "routing is unavailable for this session."
    )
    return
end

local gameplay = autoGen:WaitForChild("Gameplay", 30)
if gameplay == nil then
    warn(
        "[gameplay/server_bootstrap] ReplicatedStorage.AutoGen.Gameplay "
        .. "missing — adapter runtime can't initialize."
    )
    return
end

require(gameplay)
