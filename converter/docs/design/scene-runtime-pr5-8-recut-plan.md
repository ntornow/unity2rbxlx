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
| **PR5 canary cleared (SimpleFPS plays under generic)** | **NO** | Turret crashes; HudControl excluded → dead HUD (see below) |
| PR6 completion guard | **Not landed** | no `_check_runtime_playability_guard` / `--allow-nonplayable-output` on `main` |
| PR7 default flip → `auto` | **Not done** | default still `scene_runtime_mode="legacy"` (`core/conversion_context.py`) |
| PR8 retirement | **Not done** | `scaffolding/fps.py` + the 4 camera/control FPS packs still present |

## The two canary failures (empirically reproduced, generic SimpleFPS)

Both are transpilation/lowering/classification defects on **already-transpiled source** — **not** missing scaffolding. The HUD UI and the Turret spatial structure both convert correctly.

1. **Turret — `GetPivot` on a Sound.** `tBase = transform.GetChild(0)` lowers to `GetChildren()[1]`, but the converter emits the injected AudioSource→`Sound` as child index 0. Studio confirm: `Turret child[1]=Sound`; `first:GetPivot() → "GetPivot is not a valid member of Sound" "…CliffTurrets.Turret.Sound"`. `Base/Weapon/Origin` exist *after* the Sound. Generic runs no coherence packs, so the legacy `unity_transform_child_index` / `turret_canonical_spatial_child` fix never fires.
   - **Fix (Slice T):** deterministic, structure-gated child-index lowering — resolve `transform.GetChild(n)` to the nth `_SceneRuntimeId`-stamped child (fallback nth `BasePart`/`Model`). New pass in the lowering layer, wired via `contract_pipeline.py`. **Independent of PR8** (PR8 does not delete the turret packs — verified).

2. **HudControl — dead HUD (not the old crash).** Current planner marks it `domain="excluded"` (`fail_closed_reason="both_side_api"`: `instance_owner_is_ui`+UnityEngine.UI [client] vs `roblox_server_api` [server]). The boot loop skips `excluded` (`scene_runtime.luau`), so it never runs. The `HUD` ScreenGui + `Module/Ammo/Cur/Total/Health/ItemModule` are all present in PlayerGui — nothing drives them. The reported `FindFirstChild`-nil crash reflects an *earlier* state and no longer reproduces.
   - **Fix (Slice H):** the domain authority is now **`scene_runtime_topology/module_domain.py`** (the 7-rule classifier; `scene_runtime_domain.py` is a back-compat **shim** since PR#148 Phase 1 — *not* the fix site). `module_domain.py:324` literally cites `HudControl` as the canonical "obvious client module wrongly sent to `excluded`." The generic fix is the **classifier rule** (a UI controller — `instance_owner_is_ui`/`ui_signal` — resolves to **client**, not `both_side_api`-excluded), **not** a per-project `domain_overrides` entry (that escape hatch exists but would violate the no-hardcoding rule). **Must satisfy the now-fail-closed `contract_verifier` (PR#148 Phase 3):** flipping the domain is insufficient — Slice H must yield a *coherent* topology (client domain **+** client-reachable container **+** LocalScript class) or the verifier trips `client-in-server-container` / `server-localscript`. Then confirm `self.gameObject` binds to the `HUD` ScreenGui once it boots.
   - **Secondary:** the `Pause` panel isn't converted (`HUD.Pause` absent → `_pauseMenu()` nil) — UI-conversion completeness item, track separately.
   - **Also seen at boot:** `[scene_runtime] connectGameObjectSignal: no touch part on nil` — a component bound to a nil gameObject (likely a prefab-attached MB); fold into the canary sweep.

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
   - **Slice T** — Turret child-index lowering (lowering layer + `contract_pipeline.py`).
   - **Slice H** — HudControl UI-controller domain rule in **`scene_runtime_topology/module_domain.py`** (the post-PR#148 authority, not the `scene_runtime_domain.py` shim) + coherent container/LocalScript so `contract_verifier` passes + binding confirm.
   - Re-verify SimpleFPS under generic in Studio (Turret aims/fires, HUD updates), compare vs legacy.
   - *(If `auto` mode is still wanted in PR5:)* re-author `_check_auto_fail_closed` / `detect_fail_closed_signals` against today's fail-closed surface — the #131 versions are stale.
2. **PR6 — completion guard (fresh).** Self-contained: `_check_runtime_playability_guard` + `--allow-nonplayable-output` at the front doors + persisted ctx field. Port the stale `test_scene_runtime_pr6_playability_guard.py` (908 L) as the executable spec; write the method against today's `pipeline.py`.
3. **PR7 — flip default `legacy → auto`** (legacy retained as per-run escape hatch).
4. **PR8 — retirement (re-derive deletions).** Deletion-only: drop `scaffolding/fps.py`, the 4 camera/control FPS packs (`fps_camera_yaw_from_player_pivot`, `fps_camera_pitch_inversion`, `fps_default_controls_off`, `fps_e2e_mouse_channel`) + `fps_weapon_mount_inject`, the `--scaffolding` flag, and the `ConversionContext`/`Pipeline` scaffolding plumbing, per the contract-doc PR8 row. The 294-commit-old diff is worthless for a delete PR.

## Disposition of the draft branches
- **Close #131 / #132 / #133** as design-reference (not merged).
- **Keep as spec:** `scene-runtime-contract.md` PR5–8 rows, `scene-runtime-pr5-followups.md`, `scene-runtime-pr6-followups.md`, and `test_scene_runtime_pr6_playability_guard.py`.
