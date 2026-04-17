# Phase 4a.5: Storage Classification

> **Last verified:** 2026-04-16. Cross-check `converter/converter/storage_classifier.py` before acting on prescriptions.

Unity has no networking model. Roblox replicates between server and client — every module and template needs an explicit container. Read after 4a.1–4a.4; this phase consumes their outputs.

## Three questions, per script and per template

1. Does the **server** need this?
2. Does the **client** need this?
3. Do **both** need this?

| Container | Server | Client | Typical contents |
|---|---|---|---|
| `ServerScriptService` | runs | — | Authoritative game logic |
| `ServerStorage` | ✓ | — | Server-only modules / templates |
| `ReplicatedStorage` | ✓ | ✓ | Shared modules, templates, RemoteEvents, `_Data` |
| `ReplicatedFirst` | ✓ | ✓ (pre-replication) | Loading screen, splash |
| `StarterPlayerScripts` | — | runs (per player) | Client controllers, UI |
| `StarterCharacterScripts` | — | runs (per character) | Character-attached LocalScripts |
| `StarterGui` | — | cloned to PlayerGui | ScreenGuis (but prefer ReplicatedStorage with Enabled=false) |

## Rules (first match wins)

### Scripts

| Signal | Container |
|---|---|
| Module `require`'d by any LocalScript | `ReplicatedStorage` |
| Module `require`'d only by Scripts | `ServerStorage` |
| Module `require`'d by both | `ReplicatedStorage` |
| Client-only API (`UserInputService`, `LocalPlayer`, `Camera.CurrentCamera`, `Mouse`) | `StarterPlayerScripts` |
| Attached to player character (per scene wiring) | `StarterCharacterScripts` |
| Name hint `*Loading*` / `*Boot*` / `*Splash*` AND runs pre-replication | `ReplicatedFirst` |
| Forced client-side by 4a.2 divergence | `StarterPlayerScripts` |
| Otherwise | `ServerScriptService` |

### Prefab templates

| Signal | Container |
|---|---|
| `:Clone()`'d by any LocalScript | `ReplicatedStorage/Templates` |
| Referenced only by server scripts | `ReplicatedStorage/Templates` (default) |
| Name hint `Admin*` / `Secret*` / `Server*`, OR referenced only by `ServerStorage` modules | `ServerStorage/Templates` |
| UI template (Canvas prefab) | `ReplicatedStorage/UITemplates` |

**Why default server-spawned prefabs to ReplicatedStorage:** Roblox replicates server-parented clones automatically, so ServerStorage isn't required for server-only spawning. ReplicatedStorage doesn't break later when client code wants the template for prediction or UI. Use ServerStorage only when the template must be hidden from clients.

### Remotes / Bindables / Assets

- RemoteEvents, RemoteFunctions → **always** `ReplicatedStorage`.
- BindableEvents → co-located with their callers.
- Assets (meshes, textures, audio) are referenced by URL, not parented. The instances that reference them (SurfaceAppearance, Decal, Sound) follow their parent.

## Ambiguity rule

**When in doubt, prefer `ReplicatedStorage` over `ServerStorage`.** Misplacing into ReplicatedStorage degrades security (a client sees something it shouldn't); misplacing into ServerStorage breaks the game (`:WaitForChild` hangs forever).

## Review

The classifier (`converter/converter/storage_classifier.py`) emits a proposed `storage_plan`. Review:

- **ServerStorage modules** — real security reason? If not, move to ReplicatedStorage.
- **Mixed-API scripts** — call graph is lying; split into two modules before transpile.
- **Templates with mixed-trust callers** — pick the most permissive container.

Override by editing `storage_plan` in `conversion_plan.json` before 4b runs.

## Output

```
storage_plan:
  server_scripts:           [-> ServerScriptService]
  client_scripts:           [-> StarterPlayerScripts]
  character_scripts:        [-> StarterCharacterScripts]
  replicated_first_scripts: [-> ReplicatedFirst]
  shared_modules:           [-> ReplicatedStorage]
  server_modules:           [-> ServerStorage]
  replicated_templates:     [-> ReplicatedStorage/Templates]
  server_templates:         [-> ServerStorage/Templates]
  ui_templates:             [-> ReplicatedStorage/UITemplates]
  remote_events:            [-> ReplicatedStorage]
  overrides_applied:        [{script, from, to, reason}]
```

Phase 4b reads this and emits each script with a `parent_path` hint. `rbxlx_writer.py` routes by `parent_path` when present, else by `script_type`.
