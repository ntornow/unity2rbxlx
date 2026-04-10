---
name: convert-unity
description: Convert a Unity game project into a Roblox place file interactively, with decision points at each phase
argument-hint: <unity_project_path> <output_dir>
allowed-tools:
  - Bash(python3 convert_interactive.py *)
  - Bash(python -m pytest *)
  - Read
---

# Convert Unity Project to Roblox

Interactive, phase-based conversion of a Unity game project into a Roblox place (.rbxlx). Pause at each decision point for human judgment — do NOT run the pipeline blindly.

All `convert_interactive.py` subcommands emit structured JSON to stdout. Redirect stderr (`2>/dev/null`) to keep output clean.

For non-interactive end-to-end conversion, prefer `python3 u2r.py convert <project> -o <out>`. This skill is for cases where the conversion needs human review at one or more phases.

## Workflow

### Step 0: Gather Inputs & Preflight

Ask the user for the Unity project path, output directory, and whether they want AI-assisted transpilation. If provided as arguments, parse from args.

```bash
python3 convert_interactive.py preflight <unity_project_path> <output_dir> --install 2>/dev/null
```

If resuming, check `python3 convert_interactive.py status <output_dir> 2>/dev/null`.

### Step 1: Discovery

```bash
python3 convert_interactive.py discover <unity_project_path> <output_dir> 2>/dev/null
```

The discover phase runs the pipeline's `parse` step: builds the GUID index, finds every `.unity` scene, and selects one (or honours `--scene <path>`). Multi-scene projects: the pipeline picks the highest-scoring "gameplay" scene heuristically (see `Pipeline.parse` in `converter/pipeline.py`).

**Decision point:** If multiple scenes, ask which to include. If parse errors, ask whether to continue.

### Step 2: Asset Inventory

```bash
python3 convert_interactive.py inventory <unity_project_path> <output_dir> 2>/dev/null
```

Runs the pipeline's `extract_assets` step — discovers every asset (texture, mesh, audio), builds the asset manifest, and pre-computes FBX bounding boxes (used as InitialSize when Studio mesh resolution is unavailable).

**Decision point:** If duplicate GUIDs, orphan metas, or unexpected asset counts, warn and ask how to proceed.

### Step 3: Material Mapping

```bash
python3 convert_interactive.py materials <unity_project_path> <output_dir> 2>/dev/null
```

Runs the pipeline's `convert_materials` step. Maps Unity `.mat` files to Roblox `SurfaceAppearance` definitions (PBR maps for albedo / normal / metalness / roughness / emission). Both Standard and URP Lit shaders are supported.

**Decision point:** For unconvertible/partial materials — accept, provide manual mappings, or skip?

### Step 4: Code Transpilation

```bash
python3 convert_interactive.py transpile <unity_project_path> <output_dir> --api-key <key> 2>/dev/null
```

Runs the pipeline's `transpile_scripts` step. C# is converted to Luau via a rule-based pass with AI fallback (Anthropic Claude). The transpiler emits scripts under `<output_dir>/scripts/`. The 6 950-line `converter/luau_validator.py` then runs on the output to fix common quality issues — see Step 4c.

If you need rule-based-only transpilation (no API key), pass `--no-ai`.

**Decision point:** For each flagged script, show C# and Luau side-by-side. Ask: Accept, Retry with AI, Edit manually, or Skip?

### Step 4c: Validate transpiled Luau

After transpilation, run the validator over the output:

```bash
python3 convert_interactive.py validate <output_dir> --write 2>/dev/null
```

Without `--write` it's a dry-run. With `--write` the validator's auto-fixes are applied in-place. The validator currently knows >50 fix categories with 200+ regex patterns; review the JSON `details` array and decide whether the fixes are sane before persisting.

### Step 4.5: Game Logic Porting (LLM Rewrite)

The AI transpiler converts each file independently. This step provides cross-file architectural awareness — ensuring state machines, component wiring, and platform-specific adaptations are coherent across the full game.

**Do NOT flatten the game into a monolithic script.** The Roblox port must mirror the original Unity project's component separation, state machine structure, and timing models.

#### Step 4.5a: Architecture Map

Read all C# scripts in `<unity_project_path>/Assets/Scripts/` and produce:

1. **State machine identification** — Find the game's state machine (often a `GameManager` with `AState` subclasses). Map out:
   - What states exist (e.g., `LoadoutState`, `GameState`, `GameOverState`)
   - State transitions: which state switches/pushes/pops to which, and what triggers it
   - What each state's `Enter`/`Exit`/`Tick` does

2. **Component ownership graph** — Map which MonoBehaviour owns references to which:
   - e.g., `GameState` → `TrackManager` → `CharacterInputController` → `Character`
   - Inspector-assigned references become constructor/config wiring in Luau
   - Identify singletons (`static instance` pattern) — these become module-level state

3. **Timing model** — Identify whether game mechanics use:
   - **Time-based**: `Time.deltaTime` for durations (simple, direct bridge mapping)
   - **World-distance-based**: e.g., jump/slide measured by `worldDistance` traveled, not elapsed time. Many endless runners scale by `(1 + speedRatio)`. The Roblox port MUST preserve this.
   - **Coroutine-based**: `StartCoroutine` + `yield return` for sequenced events (map to `task.spawn` + `task.wait`)

**Decision point:** Present the architecture map to the user. Ask: "Does this match your understanding of the game?"

#### Step 4.5b: Character, Camera & Movement Divergence

Unity is a blank canvas: no default character, camera, input, or physics. Roblox provides all of these. For each pillar below, read the Unity C# code and answer: **"Does the Unity game do this itself?"** Then decide: **"Does Roblox's default do the same thing, or do we need to override it?"**

**Character:**
- Unity: No character exists until you Instantiate one and attach scripts
- Roblox: Player gets a Humanoid rig with health, collision, animation
- Override when: the game uses a custom character controller, non-humanoid avatar, or no visible character

**Camera:**
- Unity: No camera behavior until you write a script or attach a component
- Roblox: Third-person follow camera that orbits the character
- Override when: the game uses fixed camera, rail camera, top-down, isometric, or any non-orbit view

**Input → Movement:**
- Unity: No movement until you write `Update()` + `transform.Translate()` or a CharacterController
- Roblox: WASD/mobile stick moves the character, Space jumps, Humanoid handles it all
- Override when: the game uses custom movement (auto-run, on-rails, grid-based, turn-based, vehicle, etc.)

**Character positioning:**
- Unity: `Transform.position` is set by the scene or by code. No "default spawn."
- Roblox: Spawns at a `SpawnLocation` or the origin
- Override: After anchoring HRP and disabling default movement, set `hrp.CFrame` to the game's starting location. Without this, the avatar floats at the default spawn point.

For each pillar where the Unity game diverges:
- Identify exactly what the Unity code does (e.g., "TrackManager sets character position each frame from a spline curve")
- Decide how to override the Roblox default (e.g., "Anchor HumanoidRootPart, set WalkSpeed=0, drive CFrame from script")
- If too complex to port fully, design a simpler approximation that preserves gameplay feel

**This is a design decision, not a checklist.** Present to the user and ask which approach they want for each pillar.

#### Step 4.5c: Scale & Positioning

Unity uses 1 unit ≈ 1 meter. Roblox uses studs (`STUDS_PER_METER = 3.571`, see `core/coordinate_system.py`). Roblox avatars are ~5.5 studs tall vs Unity's ~1.8 units.

**Decision framework — pick one:**
- **Scale character down** (preferred for dense scene geometry like endless runners): `character:ScaleTo(SCALE)` with `SCALE = unity_character_height / roblox_avatar_height` (typically 0.2–0.3). Also adjust: `laneOffset`, `groundY`, camera offset, road/lane stripe geometry.
- **Scale world up**: Multiply all positions/sizes by a uniform factor. Simpler but requires re-running the converter and may break mesh proportions.
- **Hybrid**: Scale gameplay values without changing visual scale. Quickest hack but produces visual mismatch.

**Implementation for "scale character down":**
1. Measure Unity character height from collider or mesh bounds
2. Compute `SCALE = unity_height / roblox_height`
3. Bootstrap: `character:ScaleTo(SCALE)`, wait `task.wait(0.1)` for physics, then anchor HRP
4. `GROUND_Y = default_hrp_height × SCALE` (e.g., 2.5 × 0.25 = 0.625)
5. Pass `groundY` to character controller; pass Unity's original `laneOffset` to TrackManager
6. Scale camera offset proportionally
7. Scale road/lane stripe widths to match Unity lane geometry
8. **Do NOT scale runtime-spawned content by default.** When the character is scaled down, both the character and the converted world geometry are at Unity scale — the character was the only thing out of proportion. Cloned templates from ReplicatedStorage are already at the correct Unity scale. Scaling them by the character scale factor makes them too small. **Only scale spawned content if the Unity game explicitly scales instantiated objects in code.** **`Model:ScaleTo()` only works on Models, not individual BaseParts.** If you do need to scale, use a helper:
   ```lua
   if clone:IsA("Model") then clone:ScaleTo(SCALE)
   elseif clone:IsA("BasePart") then clone.Size = clone.Size * SCALE end
   ```

**Pipeline detail:** Unity stores transforms as local-space. The converter computes world-space positions recursively in `converter/scene_converter.py` (`world_pos = parent_pos + parent_rot * local_pos`). Scale accumulates hierarchically (`world_scale = parent_scale × node_scale`). If objects cluster at the origin, check the world-transform composition there.

**Mesh bounding box sizing and FBX units.**
MeshPart sizes are derived from the FBX bounding box, scaled by the FBX `UnitScaleFactor` and Unity's `.fbx.meta` import settings (`globalScale`, `useFileScale`). Three things must be correct for mesh sizes to match Unity:
- **UnitScaleFactor** — stored in the FBX binary; `1.0` = centimeters (scale by 0.01), `100.0` = meters (scale by 1.0). Default assumption of 0.01 produces meshes 100× too small for USF=100 files.
- **Unity import scale** — `useFileScale=1` → `globalScale × USF/100`; `useFileScale=0` → `globalScale` alone. Read from the `.fbx.meta` YAML sidecar.
- **Parent scale chain** — Unity applies scale hierarchically. A mesh at node scale (1,1,1) under a parent with scale (0.2, 0.2, 0.2) renders at 20% size. The converter passes parent world scale through `node_to_part()` recursion to accumulate this. If a mesh appears at the correct position but wrong size, check whether its parent hierarchy has non-unit scales.

**Decoration positions are baked into Unity prefabs — preserve them faithfully.**
Unity segment prefabs have all decoration children pre-positioned by the artist at specific local offsets. There is no runtime repositioning of individual decorations in Unity. Never override or "fix" these positions in the converter output. If decorations appear to block the road, the root cause is elsewhere (camera angle, character scale, mesh orientation) — not the positions.

**Mesh facing direction.**
The converter passes positions and rotations 1:1 from Unity to Roblox via the coordinate transform in `core/coordinate_system.py` (Unity LH Y-up → Roblox RH Y-up: position `(x, y, z) → (x, y, -z)`, quaternion `(qx, qy, qz, qw) → (-qx, -qy, qz, qw)`). FBX mesh geometry baked into the file may still face the wrong direction in Roblox. After conversion, visually verify decoration meshes. If roadside objects face into the road instead of away from it, apply a 180° Y-axis rotation to off-center decoration MeshParts at spawn time:
```lua
local Y_FLIP = CFrame.Angles(0, math.pi, 0)
local rot = (desc.CFrame - desc.CFrame.Position) * Y_FLIP
desc.CFrame = CFrame.new(pos) * rot
```
This is not always needed — it depends on how the original meshes were authored. Test visually before applying.

**Decision point:** Present scale approach. Ask which strategy the user wants.

#### Step 4.5d: Game Loop & Timing Rules

These are universal mechanical rules needed before writing any module code.

**Game loop wiring:**
- Unity implicitly calls `Update()`, `FixedUpdate()`, `LateUpdate()` every frame on all active MonoBehaviours
- Roblox has no implicit per-frame callbacks. A method named `Update()` that isn't connected to anything will never execute.
- **Always override.** Add `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)`. Without this, the game appears frozen — no movement, no spawning, no scoring. Disconnect in cleanup paths (`End()`, `OnDisable()`, `Destroy()`).

**Threading / yielding:**
- Unity coroutines (`yield return`) run cooperatively within the main thread. `Update()` never yields.
- Roblox signal callbacks (`Heartbeat:Connect`, `Touched:Connect`) **cannot yield**. `task.wait()` inside a callback silently stops execution — no error, no warning.
- **Rules:** (1) No-yield methods → plain functions. (2) Yielding methods → `task.spawn(function() ... end)`. (3) **Never use `coroutine.wrap` + `task.wait()` together** — `coroutine.wrap` creates a raw Lua coroutine, not a Roblox thread. If the body contains `task.wait()`, it will not resume properly.

**Array indexing (0-based vs 1-based):**
- The transpiler converts access (`arr[i]` → `arr[i + 1]`), but must NOT convert default/initial values of index variables.
- If C# has `usedTheme = 0` and the transpiler changes it to `1`, then `themes[usedTheme + 1]` becomes `themes[2]` — off-by-one returning `nil`.
- **Rule:** Index variables keep their C# value (0-based); the +1 lives only in array subscript expressions.

**Part size limits:**
- Roblox Parts **silently fail to render** if any dimension exceeds 2048 studs. No error, no warning.
- The pipeline already caps part sizes at 2048 in `converter/scene_converter.py`. If you add new geometry post-conversion, respect the same cap.

#### Step 4.5e: Assets, Visibility & Data

**Asset loading via headless place builder:**
- u2r's preferred path is the **headless place builder** (`roblox/luau_place_builder.py`): a 700KB Luau script that runs once via Open Cloud `execute_luau` to reconstruct the place using `CreateMeshPartAsync` + `SavePlaceAsync`. This embeds proper mesh geometry directly into the published place, so meshes render in Studio edit mode without any runtime loader script.
- For environments where headless publishing is unavailable, the .rbxlx still works with a runtime `MeshLoader` Script that uses `InsertService:LoadAsset()` and clones MeshParts into placeholders. See `references/upload-patching.md` for the runtime pattern.

- **The bootstrap MUST wait for MeshLoader completion** before entering gameplay (runtime loader path only). Use polling, not `Changed:Wait()`:
  ```lua
  local done = ReplicatedStorage:WaitForChild("MeshLoaderDone", 120)
  if done and done:IsA("BoolValue") and not done.Value then
      while not done.Value do task.wait(0.1) end
  end
  ```
- **Skinned meshes** (FBX with bone data from SkinnedMeshRenderer) are invisible as static MeshParts. The pipeline handles skinned meshes via Motor6D bone chains in `converter/scene_converter.py`. If a mesh is invisible despite correct MeshId/Size/Transparency, check `assimp info <file>.fbx` for `Bones: N > 0`.

**Object visibility:**
- Unity: only GameObjects with MeshRenderer or SkinnedMeshRenderer are visible. Everything else is invisible by design.
- Roblox: every Part is visible by default.
- **Always override.** The pipeline sets `Transparency=1` on objects without a renderer. Trigger colliders, inactive GameObjects, and disabled renderers are all handled.
- **Visibility is per-node, not inherited.** A parent without MeshRenderer gets `Transparency=1`, but its children with MeshRenderer keep `Transparency=0`. Do NOT add workarounds that force child MeshParts visible; the converter handles this correctly.
- If opaque gray rectangles block the view: check (1) SpriteRenderer nodes not hidden, (2) Quad/Plane surfaces not hidden, (3) MeshLoader race condition.

**ScriptableObject data:**
- The converter transpiles `.asset` files to `_Data.lua` ModuleScripts, but data still contains raw GUIDs and `nil` placeholders.
- GUIDs must be mapped to Template names in ReplicatedStorage. The data-loading code must find the data modules and be called before game start.

**Database initialization order:**
- If the bootstrap skips UI states (loadout/shop screens), database initialization those states trigger will be missed. Scripts get `nil`.
- The bootstrap must call all `LoadDatabase()` functions explicitly before the game state.

**ScreenGui placement:**
- `StarterGui` children are auto-cloned to PlayerGui on every character spawn.
- **Always** place converted ScreenGuis in `ReplicatedStorage` with `Enabled = false`. The state machine parents GUIs to `PlayerGui` when needed. Never place converted UIs in StarterGui.

#### Step 4.5f: Input, Physics & Runtime Content

**Input wiring:**
- Unity polls input via `Input.GetKeyDown()` in `Update()`. No setup required.
- Roblox uses `UserInputService.InputBegan`/`InputEnded` signals. No polling API. The transpiler does NOT create signal connections.
- The bootstrap must connect signals to dispatch input to controller methods. Map `Input.GetKeyDown(KeyCode.X)` to `Enum.KeyCode.X`. Without this, the game appears frozen to player input.

**Physics:**
- Unity: Rigidbody is opt-in, gravity/collision configured per-object
- Roblox: All parts have physics, character has Humanoid physics with WalkSpeed/JumpPower
- The converter maps Rigidbody/Rigidbody2D → `Anchored`/`CanCollide` + `CustomPhysicalProperties` (mass/drag/friction). Joints map to constraints (FixedJoint→WeldConstraint, HingeJoint→HingeConstraint, etc.)
- Override when: the game positions objects directly via CFrame/Transform rather than through physics forces

**Runtime content generation:**
- Many Unity games generate content at runtime (procedural levels, spawned obstacles, track segments). The transpiler often strips or stubs spawning logic because it depends on Inspector-serialized prefab references and object pooling.
- After transpilation, verify the core game loop actually spawns content — check for empty `m_Segments` arrays, missing `Instantiate` calls, nil prefab references. This is the **#1 reason** a converted game "runs" (score ticks, character moves) but looks empty.
- Porting pattern: convert prefab templates to Models in ReplicatedStorage/Templates, replace `Instantiate()` with `:Clone()`, resolve ScriptableObject GUIDs to Template names, wire spawning check into Heartbeat. See Step 4.5h.

**Decision point:** Present the full divergence analysis (steps 4.5b–f) to the user.

#### Step 4.5g: Animation, Particles & Implementability

1. **Transform animation detection** — Two distinct paths:
   - **Legacy Animation** (classID 111): `.anim` files driving looping transforms (spin, bob, tilt). Auto-converted by `converter/animation_converter.py` into TweenService Luau scripts.
   - **Mecanim Animator on skinned meshes**: Pipeline converts the controller into runtime config tables and uses Motor6D bone chains for skeletal animation. For animations that would otherwise be lost, generate root-motion configs from animation FBXes.

2. **Particle emission classification** — For each ParticleSystem, determine if it's continuous (ambient) or burst-triggered. Burst particles have `rateOverTime ≈ 0` with burst entries. The converter sets burst emitters to `Enabled = false` with a `BurstCount` IntValue. Game scripts must call `emitter:Emit(burstCount)` at the right moment.

3. **Player character animation state mapping.** Unity games with a player character typically use a Mecanim Animator Controller with states like Run, Jump, Slide, Fall, Death. The converter strips the skinned mesh and uses a standard Roblox R15 humanoid instead — but the **animation state transitions** must still be wired manually in the bootstrap. For each Unity animation state on the player character:
   - Read the Animator Controller (`.controller` YAML) to enumerate all states and their transition conditions
   - Map each state to a Roblox R15 default animation or catalog animation:
     | Unity state | Roblox animation | Asset ID |
     |---|---|---|
     | Run/Sprint | R15 default run | `rbxassetid://507767714` |
     | Jump | R15 default jump | `rbxassetid://507765000` |
     | Slide/Crouch | R15 default fall (as crouch) | `rbxassetid://507897817` |
     | Fall | R15 default fall | `rbxassetid://507897817` |
     | Death | R15 default fall | `rbxassetid://507897817` |
   - In the Heartbeat loop, track each state with `wasX`/`isX` flags and play/stop the corresponding AnimationTrack with crossfade (`Play(0.1)` / `Stop(0.1)`)
   - Set animation priorities: base locomotion at `Action`, overlays (jump, slide) at `Action2`
   - **Every Unity animation state must have a Roblox equivalent** — missing states produce a character that snaps to T-pose during that action.

4. **Implementability check** — For each Unity system, assess whether it can be ported as-is or needs simplification. A working simple version beats a broken complex one. If a system cannot be ported fully, implement an approximation and document what's missing.

#### Step 4.5h: Module-per-Component Rewrite

For each major game system, write a **separate Luau module** that mirrors its Unity counterpart. u2r ships several runtime modules under `runtime/` that act as bridges (animator, nav mesh, event system, physics bridge, cinemachine).

| Unity class | Roblox module |
|---|---|
| `GameManager` + `AState` subclasses | State modules + bootstrap wiring |
| `TrackManager` | `TrackManager.lua` |
| `CharacterInputController` | `CharacterController.lua` |
| Game-specific MonoBehaviours | One module per behaviour |
| Legacy Animation on non-skeletal objects | Auto-generated TweenService scripts |
| Mecanim Animator on skinned meshes | Motor6D bone chain + animation state machine |
| ParticleSystem (burst effects) | ParticleEmitter with `Enabled=false` + `BurstCount` tag |

**Rules for each module:**
- Preserve the same public API shape as the Unity class (methods, properties)
- Inspector fields → config table passed to constructor
- `GetComponent<T>()` / singleton access → explicit references passed in during wiring
- Component-to-component references → set during bootstrap, same as Unity's Inspector drag-and-drop
- **Never merge two Unity classes into one Luau module** — if they were separate in Unity, they stay separate

**Unity→Luau semantic gaps to catch during transpilation:**

The AI transpiler translates C# syntax but can miss platform-level semantic differences. The Luau validator (`converter/luau_validator.py`, 6 950 lines, 50+ fix categories) catches many of these automatically. The categories below are the ones most likely to need human review:

1. **MonoBehaviour lifecycle vs explicit construction.** Unity components are never `new()`-ed in code — they're attached to GameObjects and their fields are populated by the Inspector. The transpiler converts these to `ClassName.new(config)` constructors, but callers may not know what config to pass. *Decision:* All constructors must start with `config = config or {}` and default every field.

2. **C# properties → Luau has no `property()`.** C# `get`/`set` accessors have no Luau equivalent. If a property is trivial, use a direct field. If it has side effects, use getter/setter methods. Never emit `property()` calls.

3. **Binary serialization → table fields.** Unity often persists data via `BinaryWriter`/`BinaryReader`. Roblox uses DataStore (JSON via Lua tables). Replace `writer.Write(x)` / `reader.Read()` with `data.field = x` / `x = data.field`.

4. **Cross-module exports.** When a module returns `{ ClassA = ClassA, EnumB = EnumB }`, access the export directly: `Module.EnumB`, not `Module.ClassA.EnumB`. The export table is flat — classes don't own sibling exports.

5. **`GetComponent<T>()` on cloned objects.** Adapt to Roblox's Instance hierarchy (`FindFirstChild`, `:IsA()`, or direct construction).

6. **Singleton accessor functions vs properties.** In C#, `PlayerData.instance` is a static property. The transpiler converts this to a module export `instance = getInstance` — a **function**, not a value. All singleton accessors must be called: `Module.instance()`, not `Module.instance`.

7. **Unity MonoBehaviour lifecycle → Luau explicit calls.** Unity implicitly calls lifecycle methods. Roblox has no equivalent — all lifecycle calls must be explicit:

   | Unity lifecycle method | Roblox equivalent |
   |---|---|
   | `Awake()` | Call in constructor `.new()` |
   | `OnEnable()` | Call explicitly after construction + wiring |
   | `Start()` | Call explicitly after `OnEnable()` |
   | `Update()` | `RunService.Heartbeat:Connect(function(dt) obj:Update(dt) end)` |
   | `FixedUpdate()` | `RunService.Stepped:Connect(function(dt) obj:FixedUpdate(dt) end)` |
   | `LateUpdate()` | `RunService.Heartbeat:Connect()` (lower priority) |
   | `OnDisable()` | Call explicitly during cleanup |
   | `OnDestroy()` | Call explicitly, or use `Instance.Destroying` signal |
   | `OnTriggerEnter/Exit()` | `part.Touched` / `part.TouchEnded` signals |

   **Key pitfall:** The transpiler may rename or merge lifecycle methods inconsistently. The bootstrap must read each module's actual method names — never assume a standard name exists.

8. **C# property getters transpiled as function aliases — the silent killer.** C# properties like `public float speed { get { return _speed; } }` get transpiled as a getter method (`getSpeed()`) plus a class-level alias: `MyClass.speed = MyClass.getSpeed`. This makes `instance.speed` return the *function itself*, not the value. **This must be applied to EVERY class with properties.** Cascading failures look like:
   - `#instance.segments` → "attempt to get length of a function value"
   - `if not instance.isRerun` → always false (function is truthy)
   - `instance.score + 1` → "attempt to perform arithmetic on a function value"

   **Fix:** Replace class-level aliases with a `__index` metamethod that calls getters automatically:
   ```lua
   local _getters = {}
   local _setters = {}
   MyClass.__index = function(self, key)
       local getter = _getters[key]
       if getter then return getter(self) end
       return MyClass[key]
   end
   MyClass.__newindex = function(self, key, value)
       local setter = _setters[key]
       if setter then setter(self, value) return end
       rawset(self, key, value)
   end
   ```

9. **Inspector-serialized ScriptableObject references are nil at runtime.** In Unity, a MonoBehaviour field like `public ThemeData tutorialThemeData` is populated by the Inspector. In Roblox, the field stays nil. **Fix:** wire references through database lookups in the bootstrap or constructor.

10. **State-managed scene objects require explicit wiring.** Unity state machines toggle scene objects' visibility as part of state transitions. These scene object references come from Inspector serialization. **Fix:** the bootstrap must `workspace:FindFirstChild(...)` each scene object and pass it to the state's config. **Never use `obj.Parent = nil` to hide objects.**

11. **SetActive must use a helper, never Parent assignment.** Unity's `GameObject.SetActive(bool)` toggles visibility. Convert to a helper that sets `Transparency=1` and `CanCollide=false` (and reverses on enable). **Never emit** `obj.Parent = nil` as a visibility toggle.

**Timing model preservation:**
- If Unity uses `trackManager.worldDistance` to measure jump/slide progress, the Roblox port must too
- If Unity scales durations by `(1 + speedRatio)`, the Roblox port must too
- Do NOT simplify world-distance timing into time-based timing — it changes gameplay feel

**Porting procedural content / runtime spawning systems:**

Many Unity games generate gameplay content at runtime — spawned enemies, level chunks, projectile pools, procedural terrain, collectible placements. This is the #1 system that **does not survive transpilation** because it depends on Inspector-serialized prefab references, Addressables async loading, and object pooling.

**What spatial data does NOT survive conversion:**
- **Path/spline data** (child Transforms defining waypoints) — the converter strips non-rendered objects. Do not write auto-discovery code that walks a Model's children looking for waypoints.
- **Normalized position values** (e.g., obstacle spawn positions stored as 0–1 t-values along a path) — meaningless without the original path geometry.
- **Collider-only geometry** (trigger volumes, invisible walls) — stripped or made transparent.

**What DOES survive:**
- **Template Models** in ReplicatedStorage/Templates preserve their Unity prefab names and visible mesh hierarchy.
- **UnityLayer attributes** set by the converter on scene-placed instances. Use `part:GetAttribute("UnityLayer")` for collision classification.
- **ScriptableObject data** converted to `_Data.lua` ModuleScripts — but contains raw GUIDs that must be resolved to Template names.

**Porting pattern:**

1. **Identify templates.** The pipeline produces Models in `ReplicatedStorage/Templates` from Unity prefabs. **Never auto-discover templates** by scanning for child structure patterns — use the known prefab names.

2. **Extract per-template metadata from Unity prefab YAML.** Read `.prefab` files to determine: template dimensions/length, sub-object spawn positions, which sub-templates can appear within a template. Hardcode this metadata in a Luau table.

3. **Write spawn logic that `:Clone()`s templates** and positions them in world space. Replace Unity's `Instantiate()` with `:Clone()` + `Parent = workspace`. Resolve ScriptableObject GUID references to Template names.

4. **Implement cleanup.** When spawned content moves past a threshold distance from the player, `:Destroy()` it.

5. **Wire into the game loop.** Spawning checks must run every frame via the Heartbeat connection.

6. **Create ground/environment surfaces explicitly if needed.** Only do this when the Unity game's ground is genuinely missing — if Unity generates ground at runtime, port the generation system rather than substituting a static surface.

**Movement direction — match the converter's coordinate system:**
- The converter places objects at their Unity world positions with the (x, y, -z) axis flip from `core/coordinate_system.py`. Unity's forward axis is +Z; in Roblox that maps to -Z after the flip.
- The game loop's movement direction **must match** the axis the converted objects are placed along.
- **Camera placement:** Use absolute world-space offsets rather than rotation-relative offsets. Rotation-relative offsets break when the character has a fixed facing rotation.

**Diagnostic:** If a converted game "runs" (score increments, character animates) but the world is empty — no spawned content, no obstacles, no collectibles — the spawning system was not ported.

#### Step 4.5i: Bootstrap Wiring

Write a `GameBootstrap.lua` (LocalScript in StarterPlayerScripts) that:
- Creates instances of each module — **always pass `{}` even if no config is needed** (constructors expect a table, not nil)
- Wires cross-references **after** construction
- Registers states with the state machine
- Starts the state machine with the initial state
- Does NOT contain game logic — it's pure wiring
- **Verify method names match — CRITICAL.** The transpiler converts each C# file independently, so method names may diverge between modules. Luau silently returns nil for missing methods — no error, no warning. **Before writing any cross-module call**, `grep` the target module for the exact method name.
- To determine what to wire: read the `.unity` scene file for serialized field references
- **Uses the player's Roblox avatar** as the game character when appropriate. Wait for `player.Character`, get `HumanoidRootPart`, disable default movement (`WalkSpeed=0`, `JumpPower=0`, `JumpHeight=0`). If Step 4.5c chose "scale character down", call `character:ScaleTo(SCALE)` **before** anchoring (then `task.wait(0.1)` for physics). **Never call `Humanoid:ApplyDescription()` from a LocalScript** — server-only API.
- **Wires input** via `UserInputService.InputBegan` — the transpiler does NOT create input bindings. Map Unity's `Input.GetKeyDown` keycodes to Roblox `Enum.KeyCode`.
- **Wires collision signals** for any module that defines `OnTriggerEnter`, `OnTriggerExit`, etc. **Choose the right mechanism based on how the part moves:**
  - **Physics-driven parts** (unanchored): use `.Touched`/`.TouchEnded` signals.
  - **CFrame-driven parts** (anchored, moved by setting CFrame each frame): `.Touched` is **unreliable**. Use `workspace:GetPartsInPart(part, overlapParams)` in a per-frame `Heartbeat` loop instead.

  For the per-frame overlap pattern, use an `alreadyHit` set to prevent duplicate triggers per object, and filter out the character's own parts via `OverlapParams.FilterDescendantsInstances`. **Skip fully transparent parts** (`Transparency >= 1.0`).

**Player spawn disambiguation.** Unity scenes often have both a "Player" prefab AND a spawn marker part also named "Player." Use `workspace:GetDescendants()` with filtering: skip any "Player" BasePart whose parent is also a Model named "Player" (that's the prefab, not the spawn marker). **Never let the controller independently search for spawn position** — the bootstrap should be the single source of truth.

**Module export unwrapping — CRITICAL.** The transpiler is inconsistent about how modules export their classes. Use a defensive unwrap helper:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end

local SomeModule = unwrap(require(ReplicatedStorage:WaitForChild("SomeModule")), "SomeModule")
```

**Implement the platform divergence decisions from Steps 4.5b–f.** For each pillar where the Unity game diverges from Roblox's defaults, the bootstrap must apply the appropriate override.

#### Output location

Write all rewritten/edited scripts to `<output_dir>/scripts/`:
- One file per module
- `GameBootstrap.lua` — the entry point that wires everything
- These replace the raw transpiled versions for core systems
- The auto-detection in `converter/scene_converter.py` infers ScriptType from source: files ending with `return <identifier>` → ModuleScript, files using `Players.LocalPlayer`/`UserInputService` → LocalScript, everything else → Script.

#### Decision point

Present each rewritten module to the user for review. Show:
- Which Unity C# class(es) it was derived from
- The ownership graph: what references it holds, what references it
- Which bridge modules it uses
- Any timing model decisions
- Ask: Accept, Edit, or Regenerate?

#### CRITICAL: Unity→Roblox Visibility Rule

**No renderer = invisible. This is non-negotiable.**

- **Unity:** Objects are invisible unless they have a renderer component.
- **Roblox:** Every Part is visible by default. A Part with no mesh renders as an opaque gray block.

The pipeline MUST set `Transparency = 1` on every converted Part that lacks a renderer component. Without this, the Roblox game will be filled with opaque gray rectangles. This is the #1 visual correctness issue in Unity→Roblox conversion.

The full visibility rules (all enforced in `converter/scene_converter.py:node_to_part`):
1. **No renderer and no mesh** → `Transparency = 1, CanCollide = false`
2. **Trigger colliders** (`isTrigger = true`) → `Transparency = 1`
3. **Inactive GameObjects** (`m_IsActive = 0`) → `Transparency = 1, CanCollide = false`
4. **Disabled renderers** (`m_Enabled = 0` on MeshRenderer) → `Transparency = 1`
5. **UI subtrees** (Canvas hierarchies) → filtered out of 3D hierarchy entirely, converted to ScreenGui
6. **Opaque material with `_Color.a = 0`** → the material mapper must **ignore** `_Color` alpha when the material's rendering mode is Opaque.

#### Key principles

- **Faithful port over workarounds** — if Unity generates content at runtime, the Roblox port must too. Never substitute a Unity runtime system with a static Roblox-side workaround.
- **Architecture preservation over code translation** — the goal is a Roblox game wired the same way the Unity game was, not a line-by-line translation.
- **Port the system, not the symptom** — when something is missing or broken, trace back to what Unity system produces it.
- Bridge modules under `runtime/` are reusable — never modify them for one game.
- Game-specific scripts are output artifacts — they live in `<output_dir>/scripts/`, not in this repo.
- Focus on the 3-5 scripts that define the core game loop; leave utility scripts as-is from transpilation.
- When in doubt about a design decision, check what the Unity code actually does.

### Step 5: Assembly

```bash
python3 convert_interactive.py assemble <unity_project_path> <output_dir> \
  --api-key <roblox_key> --creator-id <id> 2>/dev/null
```

The assemble phase runs the back end of the pipeline in order: `upload_assets` → `resolve_assets` → `convert_animations` → `convert_scene` → `write_output`. Pass `--no-upload` to skip asset upload (the .rbxlx will use placeholder URLs); pass `--no-resolve` to skip headless mesh resolution.

The output is `<output_dir>/converted_place.rbxlx`. Use `python3 u2r.py validate <output_dir>/converted_place.rbxlx` to sanity-check the resulting file.

**Terrain handling:** `converter/terrain_converter.py` uses Unity's SmoothGrid binary encoding (6-bit material + occupancy + RLE, axis swap) and supports 22 terrain materials with height-based biome model and splat map support. Falls back to a `Terrain:FillBlock` runtime script when SmoothGrid encoding can't be used.

**Decision point:** If asset uploads fail, ask whether to retry, continue without, or abort.

### Step 6: Publish

```bash
python3 convert_interactive.py upload <output_dir> \
  --api-key <roblox_key> \
  --universe-id <uid> \
  --place-id <pid> 2>/dev/null
```

This is the headless publish step. It re-runs the pipeline through `convert_scene`, then generates a Luau place builder script via `roblox/luau_place_builder.py` and executes it via `roblox/cloud_api.py:execute_luau` against the user's universe/place. The place builder reconstructs the entire place (parts, meshes, scripts, terrain, lighting, UI) headlessly so meshes render in Studio edit mode with no runtime loader required.

The first invocation requires `--universe-id` and `--place-id`. Subsequent invocations reuse the cached IDs from `<output_dir>/resolve_ids.json`.

If the place builder script exceeds 4 MB, the headless path is unavailable — fall back to `python3 u2r.py publish <output_dir>` or open the local rbxlx in Studio.

**Decision point:** If publishing fails, surface the chunk error from the JSON output. Common causes: insufficient permissions on the place, expired API key, or `place_not_published` (the user must open the experience in Studio at least once).

### Step 7: Final Report

```bash
python3 convert_interactive.py report <output_dir> 2>/dev/null
```

Writes `<output_dir>/conversion_report.json` and emits its contents on stdout. Includes per-phase stats from `core/conversion_context.py`, completed phases, asset upload errors, warnings, and the universe/place IDs.

## Error Handling

If any phase fails, show the error and ask how to proceed (retry, skip, abort). Never silently swallow errors.

Each subcommand exits with a non-zero code and emits `{"success": false, "errors": [...]}` on failure. Read the JSON; do not attempt to parse the human-readable log messages on stderr.

## Guidelines

- Be concise in summaries but thorough at decision points
- Use fenced code blocks with language tags for C# or Luau
- For large lists, summarize counts first, then offer to drill into specifics
- Remember earlier decisions — don't re-ask for the same category
- If the user just wants a one-shot run with no interactivity, run `python3 u2r.py convert <project> -o <out>` instead.

See `references/upload-patching.md` for detailed upload patching strategies and assembly internals.
