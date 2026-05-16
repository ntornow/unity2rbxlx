# Unity-to-Roblox Conversion Fidelity Plan

Status: partially shipped. Author: derived from a SimpleFPS rifle-in-FPV debugging session (2026-05-15).

- **#6 — Semantic post-transpile validator:** SHIPPED — `converter/converter/semantic_validators.py`.
- **#2 — Preserve Unity prefab root as Model `WorldPivot`:** SHIPPED — `converter/converter/prefab_packages.py` (see `TestWorldPivotPreservation` in `tests/test_prefab_packages.py`).
- **#1 — Unit conversion of Unity spatial literals:** PARTIAL — semantic validator warns on suspicious literals (e.g. small-magnitude `Vector3.new`) but does not auto-rewrite.
- **#3 — Canonical FPS weapon template injection:** NOT shipped.

The remaining items live as gaps the converter still has; the doc below is preserved as the design rationale for #1/#3 if and when they're revisited.

## Context

A multi-hour debugging session over the SimpleFPS rifle pickup surfaced four converter-level gaps. Each one independently contributed to the rifle being invisible / mispositioned / wrong-scale in first-person view. The runtime-side fix shipped in `output/simplefps-revert/scripts/Player.luau`, but the root causes are in the converter and the AI transpile path — meaning the same class of bug will recur on any FPS-style Unity project until the converter is hardened.

The four planned items, in execution order:

1. **#6 — Semantic post-transpile validator** (warnings, no behaviour change)
2. **#1 — Unit conversion of Unity spatial literals** (transpile-time fix)
3. **#3 — Canonical FPS weapon template injection** (coherence pack)
4. **#2 — Preserve Unity prefab root as Model `WorldPivot`** (prefab packaging change)

Items are ordered for ROI and risk. #6 is purely additive and tells us how widespread the issues are across all test projects. #1 catches the highest-leverage bug class. #3 eliminates AI nondeterminism in the FPS path. #2 is most invasive — it changes the contract between converter-emitted prefab templates and the runtime scripts that consume them, so we want #1 and #3 stable first so any #2 regressions are easier to localize.

What follows is a per-item plan: scope, where in the codebase, change shape, tests, risks. Each item is a single PR-sized unit unless noted.

---

## #6 — Semantic post-transpile validator

### Goal

Catch the classes of bug that "passed Luau syntax but break at runtime in Roblox-specific ways" before the user discovers them visually. Output structured warnings during conversion. No behaviour change initially — surface issues to humans, don't auto-fix.

### Why first

- Pure additive: zero risk of regressing existing conversions.
- Tells us empirically which rules fire on real test projects (SimpleFPS, Gamekit3D, ChopChop, etc.). That data informs prioritisation for #1/#3.
- Same approach as the existing `luau-analyze` syntax gate — extends it to semantics.

### Where (as shipped)

- Module: `converter/converter/semantic_validators.py`.
- Invoked from `Pipeline.write_output` (see `pipeline.py:2083`) rather than as its own pipeline phase — the validator needs the post-bind script set after `_bind_scripts_to_parts` has moved gameplay scripts onto their parts.
- Output: list of `Issue` records persisted to `conversion_report.json` under `semantic_warnings`, plus CLI surface via the `write_output` log.

### Rule set (initial)

Each rule is one function, runs against the post-transpile Luau scripts (and optionally the prefab YAML for cross-references):

1. **weapon_clone_in_character** — A `<X>:Clone()` whose `.Parent` is later set to a character descendant (`Character.Head`, `Character.Torso`, `Character.HumanoidRootPart`, or any value whose path walks through the LocalPlayer's Character), inside a LocalScript with FPS markers (`MouseBehavior.LockCenter` or `Cursor.lockState`). The clone gets hit by Roblox's built-in `PlayerModule.CameraModule.TransparencyController` every frame. Suggested fix: parent to `workspace`, drive position from `camera.CFrame` each frame.

2. **anchored_false_no_weld** — A cloned BasePart/Model set to `Anchored = false` with no `WeldConstraint`/`Motor6D` linking it to another part, in a context where it's expected to follow another object (parent is a character descendant or Camera-driven part). Risk: gravity drop. Suggested fix: `Anchored = true` + per-frame CFrame, or add a weld.

3. **suspicious_meter_literal** — Vector3 literal where all three components are in `[0.001, 5.0]` (suspected Unity meters) AND the literal is used in a spatial context (assigned to a position field, used in `:PivotTo`, used as a `CFrame.new(...)` translation), AND the script's other positional values are >= 10 (in studs). Suggested fix: scale by `STUDS_PER_METER`. This is the trigger for #1's auto-fix once #1 is implemented; for now, just warn.

4. **attachment_as_render_parent** — `<basepart>.Parent = <attachment>` where the attachment is an `Attachment` Instance. Roblox does not render BaseParts under Attachments. Suggested fix: parent to `workspace` (or a Model under workspace) and drive position from the attachment's `WorldPosition`.

5. **camera_attached_no_per_frame** — A script positions an object via `camera.CFrame * <offset>` or sets its CFrame to a camera-derived value, but there is no `RunService.Heartbeat:Connect` or `RunService.RenderStepped:Connect` updating that object. One-time placement only — object will not track view direction. Suggested fix: add the per-frame update.

6. **hardcoded_camera_height** — Camera CFrame is set from `<bone>.Position + Vector3.new(0, K, 0)` where `K` is a literal that doesn't match the Unity source's `Camera.localPosition` (read from prefab YAML). Suggested fix: use Unity's value × `STUDS_PER_METER`.

### Issue record shape

```json
{
  "severity": "warning",
  "rule": "weapon_clone_in_character",
  "script": "Player",
  "line": 457,
  "snippet": "rifle.Parent = weaponSlot.Parent",
  "explanation": "The cloned rifle ends up under the character. Roblox's built-in PlayerModule.CameraModule.TransparencyController writes LocalTransparencyModifier = 1 on close-to-camera character descendants every frame, making the rifle invisible in first person.",
  "suggested_fix": "Parent the clone to workspace and drive its CFrame from camera.CFrame in a Heartbeat hook.",
  "confidence": "high"
}
```

### Tests

- Per-rule unit test: feed a snippet that triggers + a snippet that doesn't, assert correct detection.
- Per-rule unit test: assert idempotency (running the validator twice doesn't double-report).
- Integration: run the validator against the broken `getRifle` from the SimpleFPS session (preserved as a test fixture) → assert rules 1, 2, 5 fire.
- Integration: run against the previously-staged (working) `Player.luau` → assert no rules fire.
- Regression: run against every test project's transpiled output, snapshot the rule-counts JSON. Future runs compare against snapshot to catch new regressions.

### Risks / edge cases

- False positives. Default severity is `warning`, not `error`. Add an opt-in `--strict` flag for CI that promotes warnings to errors. Tune thresholds based on real-project counts (see test snapshot above).
- The "is this Vector3 used spatially?" check (rule 3) needs careful data-flow analysis. Start narrow: only fire when the literal is immediately argument to a known spatial API (`:PivotTo`, assigned to `.CFrame`/`.Position`). Expand later.
- Rule 1 ("parent walks through Character") requires tracking variable bindings. Stay narrow: only flag obvious patterns where the parent expression literally contains `.Head`, `.Torso`, `LocalPlayer.Character`, or matches the script's own `weaponSlot.Parent` idiom.

### Out of scope

- Auto-fixing. Surface only; let humans (and item #1/#3) do the rewriting.
- AI-assisted fix loop. Possible later phase once rule confidence is high.

---

## #1 — Unit conversion of Unity spatial literals

### Goal

Vector3/Vector2 literals that represent positions originally in Unity meters get auto-scaled to Roblox studs at transpile time. Eliminates the "rifle is 3.5× too close to camera" class of bug we hit with `weaponSlot.localPosition (0.4, -0.64, 0.6)`.

### Why second

- Catches a high-leverage bug class: anything with a Unity-side `localPosition` used spatially.
- Builds on #6's `suspicious_meter_literal` rule — same detector, now with a rewrite.
- Low risk if the detection whitelist is narrow.

### Where

- New pass in `converter/converter/code_transpiler.py`, runs **after** the AI transpile + **before** `luau-analyze`. The pass mutates `RbxScript.source` in place; the syntax gate then validates the rewritten output.

### Detection whitelist (narrow, expandable later)

A Vector3 literal `Vector3.new(LIT, LIT, LIT)` is rewritten iff ALL of:
- At least one of the three components is a numeric literal (not a variable expression).
- The literal is used in one of these positional contexts:
  - Argument to `:PivotTo(...)` or `:PivotTo(... * Vector3.new(...) * ...)` (nested in a CFrame construction).
  - RHS of an assignment to a field matching `.localPosition`, `.position`, `.Position`, `.CFrame.Position`.
  - Argument to `CFrame.new(...)` immediately to the right of a CFrame multiplication (`<cf> * CFrame.new(...)`), where the outer CFrame is a camera or part CFrame. This catches the `camera.CFrame * CFrame.new(offset)` pattern.
  - RHS of a `+` operator where the LHS is a CFrame/Vector3-typed value (translation).
- At least one component has magnitude `> 0.01` (skip zero-vectors, `Vector3.zero` is never converted).
- No component has magnitude `> 50` (already in studs, don't double-scale).
- The script does not already contain the marker `-- _AutoStudsScaled`.

### Rewrite shape

1. Inject near the top of the script (after the services block):
   ```lua
   -- _AutoStudsScaled: converter scaled Unity-meter Vector3 literals to studs.
   local STUDS_PER_METER = 3.571
   ```
2. Rewrite matched literals:
   ```lua
   Vector3.new(0.4, -0.64, 0.6)
   -- becomes
   Vector3.new(0.4 * STUDS_PER_METER, -0.64 * STUDS_PER_METER, 0.6 * STUDS_PER_METER)
   ```
3. Same shape for `CFrame.new(x, y, z)` translation-only constructors (the 3-arg form).
4. Add `-- _AutoStudsScaled` marker for idempotency.

### Tests

- Unit per pattern: feed a snippet matching each whitelisted context, assert scaled output.
- Unit negative: feed `Vector3.one * 0.2` (a scale multiplier) → assert unchanged.
- Unit negative: feed `Vector3.new(100, 200, 300)` (magnitude > 50, already studs) → assert unchanged.
- Unit negative: feed `Vector3.new(0, 0, 0)` (zero vector) → assert unchanged.
- Unit idempotency: rerun the pass on its own output → assert no further changes.
- Integration: feed the SimpleFPS broken `Player.luau` → assert `weaponSlot.localPosition (0.4, -0.64, 0.6)` becomes `(0.4 * STUDS_PER_METER, -0.64 * STUDS_PER_METER, 0.6 * STUDS_PER_METER)`.
- Regression across test projects: snapshot the count of rewrites per project. Manual visual diff to confirm no false positives the first run.

### Risks / edge cases

- **C# scale literals look like positions.** `transform.localScale = new Vector3(0.2f, 0.2f, 0.2f)` is a scale, not a position. The transpiler emits the same `Vector3.new(0.2, 0.2, 0.2)`. Differentiator: the assignment target's name. Scale literals are assigned to `.localScale` / `.Scale` / passed to `:ScaleTo(...)`. The whitelist only fires on `.localPosition`/`.position`/`.Position`/`:PivotTo`/CFrame translation contexts — scale assignments are excluded by construction.
- **Direction unit vectors.** `Vector3(0, 1, 0)` as an "up" direction. The whitelist's spatial-context filter should exclude these because they're not assigned to positions — they're usually passed as direction arguments to `:Raycast` or `:Move`. Verify with a per-project snapshot.
- **Half-converted values.** If the AI already scaled some values (e.g., recognised the meter convention and inserted `* 3.571` manually), we don't want to double-scale. The magnitude bound (`< 50`) handles this for cases where the AI scaled correctly. For mid-range cases, the marker on the script blocks the second pass.
- **Future Vector3 constants extracted from prefab YAML.** If serialised fields (positions read from prefab files) get emitted as Luau literals later, those need scaling too. Out of scope for this PR; flagged for follow-up.

### Out of scope

- Vector3 expressions involving variables (`Vector3.new(myX, myY, myZ)`). The transpiler can't safely scale these without data-flow analysis. They surface as warnings via rule 3 in #6.

---

## #3 — Canonical FPS weapon template injection

### Goal

Detect the Unity FPS weapon-mount pattern in transpiled Player-like scripts, and replace the AI-emitted weapon equip code with a deterministic, known-good Roblox implementation. Eliminates AI nondeterminism in the FPS path (today's bug was AI producing a different — broken — version than a previous run's working version, on the same input).

### Why third

- Builds on #1: the canonical template uses `STUDS_PER_METER` (injected by #1) and references Unity's `weaponSlot.localPosition` (scaled by #1).
- Builds on #6: detection logic reuses the FPS-marker checks.
- The "working" template is well-understood now from this session.

### Where

- New conversion sub-phase: scan Unity scripts + prefabs for FPS weapon-mount metadata, persist into `conversion_context.json`.
- New coherence pack: `converter/converter/script_coherence_packs.py`, ordered `after=("fps_default_controls_off", "fps_rifle_inject")`.

### Part A — Metadata extraction (new pre-phase)

Hook into the existing parse / extract pipeline. For each Unity Player-like script (detected via the C# AST containing `Camera.main` + an `Instantiate(...)` of a weapon prefab):

1. Identify the weapon prefab reference (e.g. `riflePrefab` field on the player MonoBehaviour).
2. Identify the weapon mount (the Transform that the weapon gets parented to). For SimpleFPS: `weaponSlot = cam.GetChild(0)`.
3. Read the mount Transform from the Player prefab YAML — `localPosition` (meters), `localRotation`, `localScale`.
4. Read the instantiation parameters from the C# (e.g. `rifle.transform.localScale = Vector3.one * 0.2f`).
5. Persist:
   ```json
   "fps_weapon_mounts": [
     {
       "player_script": "Player",
       "weapon_template": "Rifle",
       "mount_local_position_meters": [0.4, -0.64, 0.6],
       "mount_local_rotation_quat": [0, 0, 0, 1],
       "weapon_local_scale": 0.2,
       "weapon_field": "riflePrefab"
     }
   ]
   ```

### Part B — Coherence pack

Detection (a script qualifies if):
- LocalScript.
- Contains `MouseBehavior.LockCenter` (FPS marker).
- Contains a function body that clones a Templates/ReplicatedStorage prefab AND ends with `gotWeapon = true` (or matches a generalised "equip weapon" signature: a `bool` flag flipped to `true` after the clone).
- Has a corresponding entry in `fps_weapon_mounts` from Part A.

Replacement: substitute the entire matched function body with a parameterised canonical template:

```lua
-- Auto-generated by fps_weapon_mount_canonical pack. Mirrors Unity's
-- GetRifle: weapon is a workspace child whose pose is driven from
-- camera.CFrame every Heartbeat using Unity's weaponSlot.localPosition
-- (scaled to studs).
local weaponOffset_<NAME> = CFrame.new(
    <MX> * STUDS_PER_METER, <MY> * STUDS_PER_METER, -<MZ> * STUDS_PER_METER
)
local currentWeapon_<NAME> = nil

local function equip_<NAME>()
    if currentWeapon_<NAME> then
        currentWeapon_<NAME>:Destroy()
    end
    local template = ReplicatedStorage:WaitForChild("Templates"):FindFirstChild("<TEMPLATE>")
    if not template then return end
    local clone = template:Clone()
    for _, p in ipairs(clone:GetDescendants()) do
        if p:IsA("BasePart") then
            p.Transparency = 0
            p.CanCollide = false
            p.Massless = true
            p.Anchored = true
        end
    end
    if clone:IsA("Model") then
        clone:ScaleTo(<SCALE>)
        clone:PivotTo(camera.CFrame * weaponOffset_<NAME>)
    end
    clone.Parent = workspace
    currentWeapon_<NAME> = clone
    gotWeapon = true
end
```

Plus a Heartbeat driver (or, if the script already has a Heartbeat, splice into it):

```lua
RunService.Heartbeat:Connect(function()
    if currentWeapon_<NAME> and currentWeapon_<NAME>.Parent then
        currentWeapon_<NAME>:PivotTo(camera.CFrame * weaponOffset_<NAME>)
    end
end)
```

Plus a character-respawn cleanup hook (clear `currentWeapon_<NAME>` so the next character spawn re-equips cleanly).

Tag with `-- _AutoFpsWeaponMount` marker for idempotency. The pack must NOT run if the marker is already present.

### Tests

- Unit: feed a broken AI emit (parented to Head, no Heartbeat) + corresponding metadata → assert output uses `workspace` parent, has Heartbeat, uses `STUDS_PER_METER` offset.
- Unit negative: feed a non-FPS LocalScript (no LockCenter marker) → assert no changes.
- Unit negative: feed a script that already has the marker → assert no changes.
- Integration: re-convert SimpleFPS end-to-end with NO manual fixes. Assert: rifle is visible at the expected cam-relative position. (This is the regression test for the entire session.)
- Multi-weapon: feed a hypothetical project with `riflePrefab` + `pistolPrefab` → assert both mounts get their own variables (`currentWeapon_Rifle`, `currentWeapon_Pistol`) and Heartbeat updates.

### Risks / edge cases

- **AI emits the weapon code in many shapes.** Don't anchor on function names (`getRifle`/`GetRifle`/`equipRifle`/`PickupRifle` are all possible). Anchor on the body shape: a Clone() of a Templates child + sets a `gotWeapon`-like flag. Use the metadata from Part A as the source of truth, not the script's own naming.
- **The script has unrelated post-equip logic (e.g., play a sound, update HUD).** The replacement must preserve those side effects. Implementation: keep the existing function shell + sounds + flag-flip, only replace the clone-and-place block within it. Locate the block by pattern (Clone() through Parent assignment).
- **Multiple equip paths for the same weapon.** Some games re-equip on respawn or on script reload. Make `equip_<NAME>` idempotent (`Destroy()` existing first), which the canonical template already does.
- **No metadata available** (Part A failed to find the mount). Fall back to NOT modifying the script and emit a high-severity warning via #6. Don't silently leave the broken AI output.

### Out of scope

- Non-Camera-attached weapons (e.g., held in a hand bone via SkinnedMeshRenderer). Different problem.
- Animation-driven weapon poses (recoil, reload). Future work.

---

## #2 — Preserve Unity prefab root as Model `WorldPivot`

### Goal

`Model:PivotTo(target)` on any converted prefab Model places the model so its pivot — corresponding to Unity's prefab root transform — ends up at `target`, with parts at their Unity-equivalent local offsets. Mirrors Unity's `Instantiate(prefab, target_world_pos, target_rot)` 1:1.

### Why last

- Most invasive: changes the contract between converter-emitted templates and the runtime scripts that consume them. Every script that calls `:PivotTo` on a prefab Model is affected.
- Per-part position discrepancies (FBX bounding-box-center vs. Unity FBX-stored pivot) limit how exact this fix can be. Get #1 and #3 stable first so the residual issues are easier to isolate.

### Where

- `converter/converter/prefab_packages.py:234-238` — replace the unconditional `part.cframe = RbxCFrame()` wipe.
- `converter/converter/scene_converter.py:_convert_prefab_node` — carry "Unity prefab root world coord" reference downward through the prefab tree walk.

### Current behaviour (the hack we're undoing)

```python
# Prefabs live at (0, 0, 0) as templates — scripts set CFrame
# on clone. Strip any authored world-space translation the
# prefab parser may have preserved.
from core.roblox_types import RbxCFrame
part.cframe = RbxCFrame()
```

Effect: the Model element's `WorldPivot` is written as identity at `(0, 0, 0)`. Studio's runtime falls back to the geometric center of parts when the WorldPivot is "unset-equivalent" — so `Model:GetPivot()` returns the centroid, not the root. Scripts that `:PivotTo(target)` see the model placed with its centroid at `target`, not its Unity-root-equivalent. Offsets in the script then have to compensate, and the compensation is per-prefab (per-shape).

### New behaviour

Set `part.cframe` to a meaningful CFrame: the Unity prefab root's position in the template's local coordinate frame, computed during the prefab tree walk.

Algorithm in `_convert_prefab_node`:
1. The root call (`depth == 0`) represents Unity's prefab root. Record its CFrame in the part as `(0, 0, 0)` if the parts will be positioned in a frame where the root is at origin.
2. For each child, the converter already computes the world position by composing the parent's transform. If those world positions in the Roblox template are offset from origin (e.g. SimpleFPS rifle's Handle ends up at world Y=4.37 because of FBX-internal pivot baking), then the Model's `WorldPivot` should be set to the offset such that `WorldPivot + scale * unity_local_position ≈ roblox_part_world_position`.
3. Compute the offset empirically from a known part: pick the first BasePart in the prefab, compare its Roblox world position to its Unity local position × `STUDS_PER_METER` (with Z-flip). The delta is the WorldPivot.
4. Sanity check: with this WorldPivot, compute the expected world position of every other part and compare to the actual. If variance > 0.5 studs, log a `[prefab_packages] WorldPivot drift in <prefab>: max=X.X studs` warning. This surfaces the FBX-pivot-vs-bounding-box-center class of issue.

### The deeper issue (FBX pivot vs. bounding-box center)

Roblox positions `MeshPart`s at the bounding-box center of the mesh. Unity positions GameObjects at the FBX-stored pivot. For multi-mesh FBXs (like the SimpleFPS rifle's `HornetRifle.fbx`), these differ per part — so even with a correct WorldPivot, individual parts will drift from where Unity puts them. The session measured ~0.5–1.0 stud drift on different parts.

Two fix layers:

**Layer A (this PR):** WorldPivot at the Unity root, accept per-part drift. The drift is small for most prefabs and is the existing baseline anyway — this just gives scripts a meaningful pivot to reference.

**Layer B (separate work, out of scope here):** For each MeshPart, store both the bounding-box center (for Roblox `Position`) AND the FBX-stored pivot (as a `PivotOffset` so `Part:GetPivot()` returns the FBX pivot). Scripts that care about precise sub-part placement get the right answer. This requires reading FBX pivot data from the mesh resolution phase.

### Tests

- Unit: convert a synthetic prefab with a single part at Unity local `(0, 1, 2)`. Assert the resulting Model's `WorldPivot` equals `(0, 1 * STUDS_PER_METER, -2 * STUDS_PER_METER)` (with Z-flip).
- Unit: convert a multi-part prefab where parts are at known offsets. Assert WorldPivot is at the implied root, and all parts are at expected world positions ± drift tolerance.
- Regression: re-convert SimpleFPS rifle. Assert `Templates.Rifle.WorldPivot.Position` is at the empirically-correct offset (close to `(0, 0.58, -0.41)` from our session's measurements). Assert the rifle template's drift warning, if any, names the specific parts.
- End-to-end: rebuild SimpleFPS Player.luau using a simpler `weaponOffset` (no per-script empirical adjustment — just Unity's `(0.4, -0.64, 0.6) × STUDS_PER_METER`). Assert visual placement matches what's working today.

### Risks / edge cases

- **The wipe was added for a reason.** Investigate the original commit message + check whether some prefab parsing path actually does leave a non-zero world translation that needs stripping. Probably for prefabs placed in a scene at non-origin positions vs. used as templates. The new code path needs to distinguish "template root, position is meaningful local pivot" from "scene placement, position is irrelevant for templates."
- **PrimaryPart interaction.** When PrimaryPart is set, `WorldPivot` is derived from `PrimaryPart.CFrame * PrimaryPart.PivotOffset`. Writing a separate `WorldPivot` on the XML may be ignored. Test: does Roblox honour the explicit WorldPivot, or recompute from PrimaryPart at load? If recompute, we need to set `PivotOffset` on the PrimaryPart instead — more invasive.
- **Existing scripts that work around the current behaviour.** Some test projects may have scripts whose offsets were empirically tuned against the current geometric-center pivot. Changing the pivot will break them. Mitigation: keep a `--legacy-prefab-pivot` flag for one release cycle, default to new behaviour, document the migration.
- **The `RbxCFrame()` default and the rbxlx writer.** The writer at `roblox/rbxlx_writer.py:872` writes `WorldPivot` only if `part.cframe` is truthy. A non-zero CFrame is always truthy; an identity may or may not be depending on `__bool__`. Verify the marshalling.

### Out of scope

- Per-part FBX-pivot recovery (Layer B above).
- Animation pivots and constraint anchors (different machinery).

---

## Cross-cutting work

### Test fixtures

Preserve the broken SimpleFPS `Player.luau` from this session as a regression fixture. It exercises all four items independently:
- Rule 1 (weapon clone in character) — `rifle.Parent = weaponSlot.Parent`
- Rule 2 (anchored false no weld) — `_p.Anchored = false` with WeldConstraints to a parent that doesn't follow camera
- Rule 3 (suspicious meter literal) — `CFrame.new(0.4, -0.35, -0.8)` (well, actually `-0.4` in this session; either way, sub-1 magnitudes)
- Rule 5 (camera attached no per frame) — single `:PivotTo(CFrame.new(weaponSlot.WorldPosition))` with no Heartbeat hook

Place it at `converter/tests/fixtures/fps_player_broken.luau`.

### Documentation updates

- `converter/docs/UNSUPPORTED.md` — add an entry for "FPS view-model pose tracking" when #3 ships, documenting what is and isn't handled.
- `converter/CLAUDE.md` — under "Bug fix protocol", add a note: bugs that recur across test projects should be addressed at #6 (validator) + #1/#3 level, not patched per-project.

---

## Sequencing & rough effort

| Item | LOC estimate | Risk | Test surface | Suggested order |
|------|--------------|------|--------------|-----------------|
| #6   | ~400         | Low  | 6 rule snippets × 2 + integration | 1st |
| #1   | ~200         | Low  | 5 unit + 1 integration | 2nd |
| #3   | ~600         | Med  | unit + multi-weapon + e2e | 3rd |
| #2   | ~300         | High | unit + regression + e2e | 4th |

Each item is intended as a single PR. They can be reviewed and shipped independently, but the test-fixture sharing means #6 lands first so #1/#3 can reuse it.
