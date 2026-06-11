# Scene-runtime PR5–PR8 re-cut plan (supersedes branches #131/#132/#133)

**Status:** decision memo · 2026-06-04 · no code in this doc
**Authority:** [`scene-runtime-contract.md`](scene-runtime-contract.md) rows PR5–PR8 remain the design of record. This note records *where main actually stands against that design* and why the three draft branches are retired in favor of re-cutting from current `main`.

## TL;DR

- The canonical order is **PR5 → PR6 → PR7 → PR8**. PR8 is **retirement-only and last**, safe only because PR5's camera/input service + deterministic-lowering layer already replace what it deletes.
- **PR6's guard is a converter-*completion* check, not a playability gate** (contract doc, "PR6's guard rail is a converter-completion check"). The playability gate is **PR5's canary** (MCP-verify SimpleFPS vs legacy), which is **not yet cleared**.
- The **Turret** and **HudControl** generic-mode defects are **PR5 canary-gate failures**, upstream of PR6/7/8 — not post-PR8 follow-ons.
- Draft branches **#131 (PR5) / #132 (PR6) / #133 (PR8)** are **294 commits behind** a rewritten foundation and are **not mergeable or rebasable** in any useful sense. **Close them; re-cut fresh from `main`.** They survive as *spec*, not code.

## Where `main` stands vs the PR5–PR8 roadmap

| Roadmap item | On `main`? | Evidence |
|---|---|---|
| PR5 deterministic-lowering layer | **Landed** | `camera_facet_lowering.py`, `movement_facet_lowering.py`, `contract_pipeline.py` present |
| PR5 camera/input service + player↔character binding | **Landed** | #175 (camera/input fidelity), #176 (player rig↔character binding) |
| PR5 `auto` mode wiring | **Not landed** | `_check_auto_fail_closed` / `detect_fail_closed_signals` absent; `auto` rejected at CLI (`u2r.py`) |
| **PR5 canary cleared (SimpleFPS plays under generic)** | **NO** | Turret crashes; HUD dead at runtime (HudControl domain classification is FIXED — see below) |
| PR6 completion guard | **Not landed** | no `_check_runtime_playability_guard` / `--allow-nonplayable-output` on `main` |
| PR7 default flip → `auto` | **Not done** | default still `scene_runtime_mode="legacy"` (`core/conversion_context.py`) |
| PR8 retirement | **Not done** | `scaffolding/fps.py` + the 4 camera/control FPS packs still present |

## The canary failures (empirically reproduced, generic SimpleFPS)

These are transpilation/lowering/classification/wiring defects on **already-transpiled source** — **not** missing scaffolding. The HUD UI and the Turret/rifle spatial structure all convert correctly.

> **2026-06-11 dual-voice re-investigation (Claude + Codex), Step-1b cold-Studio playtest.** Confirmed Slice T still fails, and added the rifle (#3) + door (#4) canary failures + a SECOND turret-bullet bug. **HudControl: the domain CLASSIFICATION is already FIXED** (`domain="client"`, Slice H done — per this run's ledger; the "excluded" framing below is stale) — any runtime-dead HUD is then a **host-runtime binding** issue (`self.gameObject`→`HUD` ScreenGui / deferred UI-host) or the pre-existing **Pause-panel** gap, NOT a classifier defect. None are Step-1b (player-embodiment) regressions — all pre-existing generic-mode gaps where the IR either *abstains* (#1) or doesn't *model/rebind* the edge (#3/#4). File:line evidence in the Step-1b run ledger (`.harness/followups.md` "IR-coverage investigation").

1. **Turret — child-index lowering abstains → `GetPivot`/`PivotTo` on a Sound (no spin, no fire).** `tBase = transform.GetChild(0)` should lower to the nth authored child, but the converter emits the injected AudioSource→`Sound` as child index 0. Studio confirm: turret children = `[HitSound(Sound), Sound(Sound), Base(Model), Collider(Part)]`, so `getTBase()` resolves to `HitSound`; `tWeapon`/`tOrigin` then chain to nil. **Sharpened root cause (2026-06-11):** `child_index_lowering.py` ALREADY implements the right fix (`__unityChild` keyed on the deterministic `_SceneRuntimeId`), but it **never fires** — its detector regex matches only *inline* `recv:GetChildren()[N]`, while the AI factored the access into a helper (`local children = inst:GetChildren(); return children[1]`, `Turret.luau:getFirstChild`). So the lowering silently abstains and the naïve helper wins. Generic runs no coherence packs, so the legacy `unity_transform_child_index` / `turret_canonical_spatial_child` fix never fires either. This is a **§3-fault-line case**: a load-bearing lowering keyed on a *fingerprint of non-deterministic AI output*, broken by a valid-but-different shape.
   - **Fix (Slice T, revised):** lower from a **stronger signal than post-transpile surface text** — anchor on the upstream parsed `GetChild(n)` IR (or detect the factored `var = recv:GetChildren(); var[N]` shape too), then resolve to the nth `_SceneRuntimeId`-stamped child (fallback nth `BasePart`/`Model`). Reuse the existing `__unityChild` helper. New/extended detection in the lowering layer, wired via `contract_pipeline.py`; extend `tests/test_child_index_lowering.py` (currently inline-shape only). **Independent of PR8.** Clears the turret SPIN and the first SHOOT blocker — but see Slice T-bullet below; it is **not sufficient alone** for shooting.
   - **Slice T-bullet (NEW, 2026-06-11): runtime-spawned clone with `parent=nil` never enters the DataModel.** Independent of the child-index bug. `Turret:EngagedShooting` calls `host.instantiatePrefab(self.turretBullet, nil, origin:GetPivot())` (parent=nil), but the generated clone service only does `clone.Parent = parent` when `parent` is truthy (`autogen.py:881-889,1035-1044`) → the bullet clone is built + lifecycle'd but never parented into `workspace`, so it's invisible/inert. The retired `PrefabSpawner.luau` helper used `parent or workspace`. **Fix:** default a nil parent to `workspace` in the generated `instantiatePrefab` clone service (autogen). (PR #145's `_SceneRuntimeId`-on-clones restamp + lifecycle IS already fixed — verified `scene_runtime.luau:1810-1903,2025-2054`; this is a separate parenting default.)

2. **HudControl — HUD dead at runtime (NOT a classifier defect; domain is FIXED).** ~~Current planner marks it `domain="excluded"`~~ **[STALE — corrected 2026-06-11]:** HudControl now classifies `domain="client"`, `script_class="ModuleScript"`, `container="ReplicatedStorage"`, `runtime_bearing`, no fail-close (the require-fallback strip + `instance_owner_is_ui` strong-client signal already landed; the run ledger marks **Slice H done**). The `HUD` ScreenGui + `Module/Ammo/Cur/Total/Health/ItemModule` are all present in PlayerGui. So if the HUD is dead at runtime, the cause is **downstream of classification**, not the old `both_side_api` exclusion (which no longer reproduces).
   - **Fix (Slice H — domain rule DONE):** the classifier fix already landed in **`scene_runtime_topology/module_domain.py`** (UI controller — `instance_owner_is_ui`/`ui_signal` — resolves to **client**; the require-resolution `GetService("ServerStorage")` no longer counts as a server signal). No further classifier work; `domain_overrides` is correctly NOT used.
   - **Residual (runtime-binding, smaller fix):** IF the Studio canary shows the HUD dead despite `domain=client`, the root cause is runtime boot/binding (SceneRuntimeClient not constructing the requireable client module, or `self.gameObject` not binding to the `HUD` ScreenGui). Capture with its real root cause; do not re-litigate the classifier.
   - **Secondary:** the `Pause` panel isn't converted (`HUD.Pause` absent → `_pauseMenu()` nil) — UI-conversion completeness item, track separately.
   - **Also seen at boot:** `[scene_runtime] connectGameObjectSignal: no touch part on nil` — a component bound to a nil gameObject (likely a prefab-attached MB); fold into the canary sweep.

3. **Rifle not held/visible — generic `weaponSlot` rebind missing (runtime-wiring, 2026-06-11).** `Player:GetRifle` parents the spawned rifle to `self.weaponSlot or self.gameObject`, but generic `Player` **never assigns `self.weaponSlot`** (the Unity `cam.GetChild(0)` line was dropped; only a comment remains, `Player.luau:60-62,193-199`) → the rifle parents to the far player body, not the camera. The target IS present: `Main Camera` carries `_MainCameraRig`, has a `WeaponSlot` child, and `CameraRigFollower` pivots that rig onto `workspace.CurrentCamera` every frame — so paradigm C's camera ownership is **not** the blocker (shooting already reads `host.player:getLookCFrame()`/`applyRecoil`). The LEGACY pack rebinds via `rig:FindFirstChild("WeaponSlot", true)` (`script_coherence_packs.py:508-551`) but generic skips packs (`pipeline.py:2961-2977`). `weaponSlot` is genuinely absent from `serialized_field_refs` (it's `[HideInInspector]` + runtime-assigned), so this is a missing **recovery/rebind**, not a dropped serialized ref.
   - **Fix (Slice R):** a deterministic generic rebind of `weaponSlot` to the `_MainCameraRig`/`WeaponSlot` Instance by name (the generic equivalent of the legacy pack), in the lowering layer. (Not yet confirmed whether `Templates.Rifle` is a stripped/invisible variant — verify the rifle template is visible before ranking that above the rebind.)

4. **Door visual never opens — dynamic-Animator-driver IR-capture gap (the one true capture gap).** `Door` is `domain=client`, writes `doorAnim:SetAttribute("open", value)` **client-side** (`Door.luau:49-53`); the generated `Anim_Door_door_*` are **server** `Script`s bound on `GetAttributeChangedSignal("open")` — a client attribute write doesn't replicate → the tween never runs (the open *sound* is audible because client `Sound:Play` replicates locally). `animation_routing` resolves drivers only via *serialized* Animator refs (`animation_routing.py:229-346`); the Door's driver is dynamic (`transform.parent.Find("door").GetComponent<Animator>()`, no serialized ref) → `routing_status="unresolved"` → server placement. **This is the deferred Phase-2 source-narrowing** (see `scene-runtime-pr148-followups.md`).
   - **Fix (Slice D = the pr148-followups door PR):** parse the C# Animator-parameter writes (`SetBool`/`SetTrigger`/...) and match the clip's `observed_attribute` to the writer MB → resolve the driver → route the Anim script to the driver's (client) domain. **Scope caution (Codex, 2026-06-11):** treat this as a *door* fix, NOT a universal animation cure — the other unresolved `animation_drivers` entries fail for *different* reasons (`HostilePlane:Flying` is an autoplay loop with `observed_attribute=""` → no parameter to narrow on; the `PlaneHolder` entries have a cross-prefab REF gap — `Machine` writes `planeHolder:SetAttribute` but the Machine prefab refs lack the `planeHolder` ref and `PlaneHolder.prefab` has no instances/refs). Those are separate slices.

## Why the draft branches can't be merged

Merge-base `e06e435` (2026-05-21); `main` is **+294 commits** with the files these branches edit largely rewritten:

| File | base → main churn | Effect on branch edits |
|---|---|---|
| `scene_runtime_domain.py` | 695 L → **85 L** (+71/−681) | edits target code that no longer exists |
| `pipeline.py` | 4688 L (+1860) | subphases land in a restructured file |
| `scene_runtime_planner.py` | 954 L (+711) | old-classifier assumptions stale |
| `script_coherence_packs.py` | 4734 L (+652) | PR8's −536 gut conflicts |

`git merge-tree` reports content conflicts in `pipeline.py` + `scene_runtime_domain.py` on all three (+ a modify/delete on `fps.py` for PR8). Auto-resolving would resurrect pre-classifier-v2 domain logic `main` deliberately replaced. **Conflict resolution here = re-authoring against new structures = re-cut.**

## Re-cut plan (dependency order, from current `main`)

1. **Finish PR5 canary** (blocks everything downstream)
   - **Slice T** — Turret child-index lowering, from a **stronger signal than post-transpile text** (anchor on the parsed `GetChild(n)` IR / detect the factored `var=GetChildren(); var[N]` shape), reusing the `__unityChild` `_SceneRuntimeId` helper (lowering layer + `contract_pipeline.py`; extend `test_child_index_lowering.py`). Clears turret SPIN + first SHOOT blocker.
   - **Slice T-bullet** — default a nil `parent` to `workspace` in the generated `instantiatePrefab` clone service (`autogen.py`) so runtime-spawned bullets enter the DataModel. Required (with Slice T) for turrets to actually shoot.
   - **Slice H** — domain rule is **DONE** (HudControl → `domain="client"` already landed in `scene_runtime_topology/module_domain.py`). Remaining is only the runtime-binding confirm (HUD ScreenGui / `self.gameObject`) + the separate Pause-panel completeness gap — not a classifier change.
   - **Slice R** — deterministic generic `weaponSlot` rebind to `_MainCameraRig`/`WeaponSlot` by name (generic equivalent of the legacy pack), so the picked-up rifle is held/visible.
   - **Slice D** — the `pr148-followups` door driver narrowing (Phase-2 C#-source Animator-parameter resolution → route the Anim script to the client driver). Door-scoped; the other unresolved animation drivers are separate.
   - Re-verify SimpleFPS under generic in Studio (Turret aims/fires, rifle held, door opens, HUD updates), compare vs legacy.
   - *(If `auto` mode is still wanted in PR5:)* re-author `_check_auto_fail_closed` / `detect_fail_closed_signals` against today's fail-closed surface — the #131 versions are stale.
2. **PR6 — completion guard (fresh).** Self-contained: `_check_runtime_playability_guard` + `--allow-nonplayable-output` at the front doors + persisted ctx field. Port the stale `test_scene_runtime_pr6_playability_guard.py` (908 L) as the executable spec; write the method against today's `pipeline.py`.
3. **PR7 — flip default `legacy → auto`** (legacy retained as per-run escape hatch).
4. **PR8 — retirement (re-derive deletions).** Deletion-only: drop `scaffolding/fps.py`, the 4 camera/control FPS packs (`fps_camera_yaw_from_player_pivot`, `fps_camera_pitch_inversion`, `fps_default_controls_off`, `fps_e2e_mouse_channel`) + `fps_weapon_mount_inject`, the `--scaffolding` flag, and the `ConversionContext`/`Pipeline` scaffolding plumbing, per the contract-doc PR8 row. The 294-commit-old diff is worthless for a delete PR.

## Disposition of the draft branches
- **Close #131 / #132 / #133** as design-reference (not merged).
- **Keep as spec:** `scene-runtime-contract.md` PR5–8 rows, `scene-runtime-pr5-followups.md`, `scene-runtime-pr6-followups.md`, and `test_scene_runtime_pr6_playability_guard.py`.
