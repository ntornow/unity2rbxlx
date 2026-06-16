# Addressables runtime-content conversion (generic scene-runtime) — design

**Status:** draft v2 — corrected after dual review (Claude + Codex, 2026-06-15).
v1's "two gaps" framing was **wrong-anchor**; this version reflects the real
runtime identity model and the full set of subsystems gating gameplay.
**Goal (ONE sentence):** make a heavily-Addressables Unity game's runtime content
(character, consumables, obstacles, track segments, themes) spawn in the converted
Roblox place, so `host.instantiatePrefab(<id>)` and the theme/roster systems resolve
to real templates and the game plays.

## Why (motivating evidence — Trash Dash)

Trash Dash loads ~all runtime gameplay content via **Addressables**
(`InstantiateAsync` / `LoadAssetsAsync` / `AssetReference`), lowered to
`host.instantiatePrefab(<name/address>, …)` and SO/ScriptableObject data. Today
**nothing gameplay spawns**, which is the shared root of the two residual Studio
errors (CharacterCollider nil-GameObject; consumables crash).

## Current state (verified against the regenerated output + project + dual review)

1. **Discovery exists.** `scene_runtime.prefabs` has all **102** prefab subplans,
   keyed `"<guid>:<path>"`, each with `name`, `template_name`, …
2. **Emission is filtered to 2.** `prefab_packages.generate_prefab_packages` emits
   a template only for prefabs named in a script's *serialized GameObject field*
   (`_collect_referenced_prefab_names`, `prefab_packages.py:281,331`); `packages/manifest.json`
   confirms **2 of 102** emitted (`IconConsumable`, `PowerupIcon`).
3. **Runtime identity is `template_name`, and it collides.** `instantiatePrefab`
   resolves `_plan.prefabs[prefab_id]` then `clonePrefabTemplate` →
   `Templates:FindFirstChild(template_name)`; `prefab_packages` forces
   `part.name = name`. There are **33 duplicate `template_name`s** — both characters
   are `character`; day/night obstacle & segment pairs reuse names. A flat
   `Templates` folder + bare-name lookup picks the **wrong** prefab.
4. **Not all spawns go through `instantiatePrefab`.** Dynamic call sites bypass the
   host (`LoadoutState.luau:294`, `PatrollingObstacle.luau:61`, `AllLaneObstacle.luau:30`).
5. **Address map exists but isn't a flat dict.** `AssetGroups/*.asset` carry
   `m_Entries:[{m_GUID,m_Address,labels}]`. Addresses repeat across groups
   (`themeData` ×2 in Themes.asset; `ObstacleBin` in both Day/Night obstacle groups),
   and groups mix prefabs with non-prefab assets (sprites, SOs, zones).
6. **Theme system is dead.** `TrackManager` dereferences `currentTheme` immediately
   (`TrackManager.luau:177,187`) but `ThemeDatabase.Register` is **never called**;
   `themeData` SOs carry `zones[].prefabList` / `collectiblePrefab` /
   `premiumCollectible` / `cloudPrefabs` as **unresolved AssetGUID tables**
   (`themeData__86f154.luau:13,111`). So segments/coins/clouds/obstacles cannot spawn
   even with templates emitted — this is a hard gate.
7. **Roster path is dead.** `CharacterDatabase.luau:33` waits on a
   `ReplicatedStorage.Characters` *folder* that's never emitted (the generated
   `Characters` artifact is a ModuleScript describing the group); `LoadoutState`
   loops on a missing character prototype.
8. **Consumables need prototype data, not a key.** `consumbales` refs point at
   powerup prefabs; the real `Consumable` serialized fields (`canBeSpawned`,
   type/price/icon) live on the `Consumable`-derived MonoBehaviour *inside* each
   prefab (`CoinMagnet.prefab:66`, `ExtraLife.prefab:53`). Consumers read
   `.canBeSpawned` (`TrackManager.luau:557`), `GetConsumableType/GetPrice/icon`
   (`ShopItemList.luau:48`, `LoadoutState.luau:357`).

## Root cause (corrected)

Not "two gaps." The converter has no model for **Addressables runtime content**:
(a) a unique, collision-free **prefab identity** the runtime can resolve;
(b) **emission** of Addressable-referenced prefabs as uniquely-keyed templates;
(c) resolution of **AssetReference / GameObject / SO object-refs** (themes,
consumables, segments) from `{guid,fileID}` to that identity; (d) **prototype
materialization** so referenced prefabs expose their component data/methods;
(e) **label loads** (`LoadAssetsAsync(label)`) for rosters; plus (f) the
transpiled call sites that **bypass** the host.

## Design (corrected)

### Phase 0 — Unique prefab identity (FOUNDATION; do first)
- Give every emitted template a **unique key** (e.g. the `<guid>:<path>` prefab_id,
  or a sanitized `name__<guid6>`), and make `clonePrefabTemplate` resolve by that
  key — NOT bare `template_name`. Fix the 33-collision class at the source.
- Disambiguate the on-disk/`Templates` child names accordingly (mirror the
  `__<hash>` scheme already used for duplicate class names / SO names).
- Audit + rewrite dynamic spawn call sites that bypass `instantiatePrefab` (or give
  them a stamped source-prefab-id) so all spawns funnel through one resolver.

### Phase 1 — Addressables discovery (build-time, deterministic)
- New `unity/addressables_resolver.py`: parse `AssetGroups/*.asset` →
  `address → [guid]` (a LIST — addresses repeat), `label → [guid]`, and
  `guid → address(es)`. Resolve guids to prefab_ids via the GUID index + the plan.
- **Type-filter** each guid via the GUID index: only GameObject/prefab addresses
  become instantiable templates; sprites/audio/SO/zone addresses route to their own
  resolvers (or are recorded, not templated). This is load-bearing, not a footnote.
- Emit a plan `addressables` block: `by_address`, `by_label`, `by_guid` — values are
  prefab_ids (the Phase-0 unique keys), with collision handling.

### Phase 2 — Emit Addressable prefabs as uniquely-keyed templates
- Extend `generate_prefab_packages` target set to
  `serialized_referenced ∪ addressable_referenced`, emitting each under its Phase-0
  unique key. Reuse the prefab→template conversion. Bloat guard: emit only
  referenced/addressable prefabs; log emitted-vs-total.

### Phase 3 — Resolve dynamic references to prefab identity
- `instantiatePrefab(id)`: resolve `id` through `_plan.prefabs` → else
  `addressables.by_address` → else fail-soft. Covers the character path
  (`"Trash Cat"`).
- **AssetReference / GameObject SO fields** (`{guid,fileID}` inside themeData,
  Consumables, TrackSegment): resolve at SO-emit time (the SO emitter is already
  guid-index aware) to the Phase-0 prefab id, so `prefabList`, `collectiblePrefab`,
  `cloudPrefabs`, `possibleObstacles`, `consumbales[i].gameObject` become real ids.

### Phase 4 — Theme registration + ThemeData/TrackSegment resolution (HARD GATE)
- Make `ThemeDatabase.Register` actually populate (re-lower the dead registration,
  or seed the theme list from the discovered `themeData` addresses) so
  `currentTheme` is non-nil.
- Resolve every themeData/TrackSegment AssetReference to a prefab id (Phase 3).
  This is the real gate for segments, coins, clouds, obstacles.

### Phase 5 — Prototype materialization for consumables (and shop/loadout)
- Resolve `Consumables.asset` refs to usable `Consumable` objects carrying both
  `.gameObject` (the prefab id) AND the component's serialized fields
  (`canBeSpawned`, type, price, icon) read from the referenced prefab's
  `Consumable`-derived component. Define an inventory-instantiation strategy
  (the original clones the prototype, `LoadoutState.cs:134`).

### Phase 6 — Roster / label loads
- Re-lower `CharacterDatabase` to enumerate `addressables.by_label["characters"]`
  into usable Character prototypes; replace the dead `ReplicatedStorage.Characters`
  folder + `LoadoutState` `Templates:FindFirstChild(characterName)` preview path.

## Phasing (regrouped — 4 units, not 6)

The front phases (0/1/2/3a) are **one coupled unit** — they share the identity
decision and none is testable alone. The later subsystems (themes / consumables /
roster) are genuinely distinct (different code, consumers, risk) and stay separate
as independently reviewable units. 3b (the guid→prefab-id resolver) is a **shared
primitive** built once and reused by the subsystems.

- **Unit 1 — Foundation + character spike** (= old Phase 0 identity + 1 discovery
  + 2 emission + 3a host `instantiatePrefab(address)`). Riskiest-first. Acceptance:
  `TrackManager:Begin` resolves `"Trash Cat"` → clones the **Cat** template (not
  Raccoon, via unique keys) → CharacterCollider boots with a real GameObject (the
  real fix for Error 2). **Also confirm `TrackManager`'s spawn domain** — a
  server-side spawn won't boot the character's client input/collider stack.
- **Unit 2 — Reference-resolution primitive** (= old 3b). Resolve AssetReference /
  GameObject / SO `{guid,fileID}` → the Unit-1 prefab id, once. Reused by Units 3–4.
- **Unit 3 — Themes** (= old Phase 4): `ThemeDatabase.Register` actually populates +
  themeData AssetReferences resolved. Gate for segments/coins/clouds/obstacles.
- **Unit 4 — Consumables + roster** (= old Phases 5+6): consumable prototype
  materialization, and `CharacterDatabase` label-load rewrite. Split if Unit 4 grows.

**Build Unit 1 first and reassess** — the spike validates the spine end-to-end and
sharpens the later estimates with real evidence.

## Scope reality

A **multi-subsystem project**, not a localized fix: prefab-identity rework +
Addressables discovery + AssetReference/SO resolution + theme registration +
prototype materialization + label loads + transpiled-call-site rewrites. ~4
reviewable units; Unit 1 is the validating spike.

## Risks / open questions
- **Spawn domain** (client vs server) of `TrackManager` — determines whether spawned
  client components boot; the actual mechanism behind Error 2.
- **Call-site coverage** — how many dynamic spawns bypass the host; do they all carry
  a resolvable id after Phase 0?
- **Generality** — `AssetGroups/*.asset` is the standard editor schema (generalizes),
  but type-filtering + duplicate-address handling must be robust per-project.
- **rbxlx bloat** — ~100 templates; size/perf.
- **Load order** — reuse the lazy/fail-open pattern from the SO method-binding fix.

## Out of scope
- Remote Addressables / content catalogs / CDN. Source-time editor groups only.

## Review record
- Claude design review + Codex review (2026-06-15): both confirmed Q1 (address ==
  `m_Address`) and the emission-filter gap; both flagged the `template_name`
  collision; Codex (verdict `wrong-anchor`) additionally surfaced the dead theme
  system, the host-bypass call sites, prototype materialization, and duplicate
  addresses. This v2 integrates all findings.
