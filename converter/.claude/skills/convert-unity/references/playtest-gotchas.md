# Playtest Gotchas

Verified-by-experience caveats for automating end-to-end gameplay checks on
converted places through the Roblox Studio MCP. If you're trying to
"prove the rifle works" or similar, read this first — every item below cost
us a debugging loop in the 2026-04-12 SimpleFPS session.

## 1. `require()` from `execute_luau` gives you a FRESH module instance

When you call the Studio MCP's `execute_luau` tool and do
`require(game.ReplicatedStorage.Player)` from inside that command-bar-like
context, Roblox hands you a **new instance** of the ModuleScript — NOT the
one the client's `LocalScript` is actively running. Upvalues like
`gotWeapon`, `curAmmo`, and the character binding are all at their default
state in your copy.

**What breaks:** any "call Player.shoot() and see if it fires" test run
from `execute_luau` will hit the early `if not gotWeapon then return end`
guard and no-op, even though the real game loop's `shoot()` closure is
fully armed.

**What works:** inject a test hook INTO a `LocalScript` inside
`StarterPlayerScripts` (e.g. append a `task.spawn` block to
`ClientBootstrap` that calls the same module-local `shoot`). The hook runs
in the same environment as the real player code, so it shares the live
upvalues.

```lua
-- Inside StarterPlayerScripts.ClientBootstrap, appended via execute_luau:
task.spawn(function()
    task.wait(5)
    local Player = require(game.ReplicatedStorage.Player)
    plr.Character.HumanoidRootPart.CFrame = CFrame.new(X, Y, Z)
    task.wait(3)
    Player.shoot()   -- Real closure, real upvalues
end)
```

See the `[ShootTest] neon parts after shoot: 1` evidence trail in the
2026-04-12 session log.

## 2. `user_mouse_input` clicks go to Studio window coordinates, not game viewport coordinates

The `mcp__roblox-studio__user_mouse_input` tool takes screen (x, y) pixel
coordinates relative to the whole Studio window, not the game viewport.
Click at `(700, 500)` when the viewport happens to start at `(200, 150)`
and you land in the edit pane, not the play area. The click will register
*somewhere*, but `UserInputService.InputBegan` in your game script won't
fire because the click went to Studio chrome.

**Workaround:** pass `instance_path: game.Workspace.Camera` or a known
GUI element to the tool — it resolves to the element's on-screen
rectangle and clicks in the middle. Or: synthesize the input with
`VirtualInputManager:SendMouseButtonEvent(...)`, but that requires
`RobloxScript` capability, which `execute_luau` does NOT have.

## 3. The Pickup `Touched` event fires *many times* per physics step

When the character walks onto a pickup's touch detector, `part.Touched`
fires on every physics step while the character overlaps the trigger —
often 10+ times before the 0.5s destroy-delay kicks in. The validator
fixes inject a `local _fired = false` debounce on the server side and a
`if gotWeapon then return end` early-exit on the client side so this
doesn't spam RemoteEvents or duplicate-equip the weapon, but your console
log will still show many `[Pickup] firing ...` lines — that's expected.

## 4. Edits to a running ModuleScript's `.Source` don't reload the closure

If you patch a script's `Source` *during* Play, the changes are saved to
disk but the already-`required()` module's cached return value is
unchanged — its closures still reference the old upvalues. To see an
edit, **stop Play, then start Play again**. The `--start_stop_play`
cycle reloads all required modules.

If you can't restart play, patch the same module in an edit-mode
`execute_luau` call first, *then* start Play — the new play session will
pick up the edited source.

## 5. Studio MCP `list_roblox_studios` can disconnect after long tasks

After a multi-minute `subagent` call (especially playtest), the
previously-active Studio instance sometimes reports
`"previously active Studio instance has disconnected"`. The instance
itself is still running; you just need to re-call `set_active_studio`
with its ID. Always re-list and re-set before the next call if you
went idle.

## 6. `.rbxlx` reloads don't happen automatically

Opening a freshly-regenerated `converted_place.rbxlx` while Studio is
already running the old version does NOT reload the place — Studio
caches the in-memory DataModel. Close the place via `File → Close Place`
(click through the "Don't Save" dialog) before re-opening, or all your
live edits will reference stale script instances.

AppleScript recipe:

```applescript
tell application "System Events"
  tell process "RobloxStudio"
    click menu item "Close Place" of menu "File" of menu bar item "File" of menu bar 1
    delay 2
    try
      click button "Don't Save" of sheet 1 of window 1
    end try
  end tell
end tell
```

Then `open /path/to/new.rbxlx` to reopen.

## 7. `character_navigation` often reports "Path Blocked"

The `mcp__roblox-studio__character_navigation` tool uses pathfinding
service to walk the character from A to B. On SimpleFPS's terrain it
frequently returns `"Path Blocked"` even when a direct line-of-sight
exists.

**Workaround:** teleport the character directly via `execute_luau`:

```lua
local plr = game.Players:GetPlayers()[1]
plr.Character.HumanoidRootPart.CFrame = CFrame.new(546, 25, -733)
```

Then let `task.wait(2)` run so the Touched event can fire.
