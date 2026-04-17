# Phase 4a.5: Storage Classification

> **Last verified:** 2026-04-16. Cross-check against `converter/converter/storage_classifier.py` before acting on prescriptions.

Unity has no inherent networking model — all code and assets live in one process. Roblox replicates data between server and client, so **every module and every template must be placed in a container that reflects who needs to read it**. This decision cannot be silent; it is the explicit output of 4a.5.

Read this file **after** 4a.1–4a.4 — the storage plan consumes their outputs.

## The three networking questions

For every script and every prefab template, answer:

1. **Does the server need this?** If yes, not StarterPlayerScripts / ReplicatedFirst.
2. **Does the client need this?** If yes, not ServerStorage / ServerScriptService.
3. **Do both need this?** If yes, ReplicatedStorage.

The answer determines the container. The container table:

| Container | Server sees | Client sees | Typical contents |
|---|---|---|---|
| `ServerScriptService` | ✓ (runs) | ✗ | Server scripts (authoritative game logic) |
| `ServerStorage` | ✓ | ✗ | Server-only modules, server-only prefab templates |
| `ReplicatedStorage` | ✓ | ✓ | Shared modules, shared templates, RemoteEvents, `_Data` |
| `ReplicatedFirst` | ✓ | ✓ (before replication) | Loading screen, splash |
| `StarterPlayerScripts` | ✗ | ✓ (runs per player) | LocalScripts (client controllers, UI) |
| `StarterCharacterScripts` | ✗ | ✓ (runs per character) | LocalScripts attached to the character |
| `StarterGui` | ✗ | ✓ (cloned to PlayerGui) | ScreenGuis — but **see universal rules**: prefer ReplicatedStorage with Enabled=false and reparent from state machine |

## Rules (apply in order; first match wins)

### Scripts

| Signal | Container |
|---|---|
| Module `require`'d from any LocalScript (per call graph) | `ReplicatedStorage` |
| Module `require`'d only from Scripts | `ServerStorage` |
| Module `require`'d from both | `ReplicatedStorage` |
| Script with client-only API surface (`UserInputService`, `LocalPlayer`, `Camera.CurrentCamera`, `Mouse`) | `StarterPlayerScripts` (as LocalScript) |
| Script attached to a player character prefab (via Unity scene wiring) | `StarterCharacterScripts` (as LocalScript) |
| Script with source-name hint `*Loading*` / `*Boot*` / `*Splash*` AND runs before replication (per architecture map) | `ReplicatedFirst` |
| Script with divergence override forcing client side (per 4a.2) | `StarterPlayerScripts` |
| Everything else | `ServerScriptService` (as Script) |

### Prefab templates

| Signal | Container |
|---|---|
| Template `:Clone()`'d by any LocalScript (per call graph) | `ReplicatedStorage/Templates` |
| Template referenced only by server scripts (per `templates_manifest.spawned_by`) | `ReplicatedStorage/Templates` (default) — unless flagged secret |
| Template with name hint `Admin*` / `Secret*` / `Server*` OR referenced only by modules in `ServerStorage` | `ServerStorage/Templates` |
| UI template (Canvas prefab) | `ReplicatedStorage/UITemplates` (state machine parents to PlayerGui) |

**Why default prefabs to ReplicatedStorage even when only the server spawns them:** Roblox replicates server-parented clones automatically, so ServerStorage isn't required for server-only spawning. ReplicatedStorage is the safer default because it doesn't break when client code later needs to reference the template for prediction or UI. Use ServerStorage only when the template genuinely must be hidden from clients.

### RemoteEvents / RemoteFunctions / BindableEvents

- RemoteEvents and RemoteFunctions → **always** `ReplicatedStorage` (Roblox requires it for cross-boundary dispatch).
- BindableEvents → same container as the scripts that use them (co-located).

### Assets (meshes, textures, audio)

Assets are referenced by URL, not parented as Instances, so they don't need a container. But the **SurfaceAppearance / Decal / Sound instances** that reference them are parented to Parts/Models. Those follow their parent's container.

## The ambiguity rule

**When in doubt, prefer `ReplicatedStorage` over `ServerStorage`.** Misplacing into ReplicatedStorage degrades security (a client can see something it shouldn't); misplacing into ServerStorage breaks the game (a client `:WaitForChild` hangs forever). The survivable default is ReplicatedStorage.

## Agent decision

Run the classifier (`converter/converter/storage_classifier.py` during the `classify_storage` pipeline phase). It emits a proposed `storage_plan`. Review:

- **Any module forced into ServerStorage** — is there a real security reason? If not, move to ReplicatedStorage.
- **Any script with both client and server API surface** — the call graph is lying; the script should probably be split into two modules before transpile.
- **Any template spawned by multiple callers with different trust levels** — pick the most permissive container (ReplicatedStorage).

Override decisions by editing `storage_plan` in `conversion_plan.json` before phase 4b runs.

## Output

`storage_plan` in `conversion_plan.json`:

```
storage_plan:
  server_scripts:           [script names → ServerScriptService]
  client_scripts:           [script names → StarterPlayerScripts]
  character_scripts:        [script names → StarterCharacterScripts]
  replicated_first_scripts: [script names → ReplicatedFirst]
  shared_modules:           [module names → ReplicatedStorage]
  server_modules:           [module names → ServerStorage]
  replicated_templates:     [template names → ReplicatedStorage/Templates]
  server_templates:         [template names → ServerStorage/Templates]
  ui_templates:             [template names → ReplicatedStorage/UITemplates]
  remote_events:            [event names → ReplicatedStorage]
  overrides_applied:        [list of agent-applied overrides with reason]
```

Phase 4b reads this and emits each script with a `parent_path` hint. `rbxlx_writer.py` routes based on `parent_path` when provided, falling back to the script_type heuristic when absent.
